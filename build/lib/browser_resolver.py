"""Capa anti-bot: resuelve desafíos con un navegador headless (Playwright).

Se activa sólo cuando el resolver HTTP detecta un challenge (Cloudflare,
Turnstile, reCAPTCHA, "Checking your browser", "I am not a robot"...). El
módulo es independiente del motor: recibe una URL y devuelve la URL final,
las cookies de la sesión y, si hubo descarga, el archivo guardado.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urljoin, urlsplit

from desenlace_core import (
    DEFAULT_MAX_WAIT,
    DEFAULT_TIMEOUT,
    DEFAULT_UA,
    UnsafeUrlError,
    ensure_extension,
    filename_from_url,
    is_plausible_url,
    release_destination,
    reserve_destination,
    sanitize_filename,
    validate_url,
)

# Frases y selectores que delatan una página anti-bot.
BOT_CHALLENGE_MARKERS = (
    "just a moment...",
    "just a moment",
    "enable javascript and cookies to continue",
    "checking your browser",
    "checking if the site connection is secure",
    "verify you are human",
    "verifying you are human",
    "i am not a robot",
    "i'm not a robot",
    "no soy un robot",
    "attention required! | cloudflare",
    "cf-challenge",
    "cf-browser-verification",
    "cf_chl_opt",
    "cf-chl",
    "ddos-guard",
)

CHALLENGE_SELECTORS = (
    "#challenge-form",
    "#challenge-stage",
    "#cf-challenge-running",
    ".cf-turnstile",
    "div[class*='turnstile']",
    "iframe[src*='recaptcha']",
    "iframe[src*='turnstile']",
    "iframe[src*='hcaptcha']",
    "input[name='cf-turnstile-response']",
)

CLICK_SELECTORS = (
    "#robot-check",
    "#recaptcha-anchor",
    "input[type='checkbox'][name*='robot']",
    "button:has-text('I am not a robot')",
    "button:has-text('No soy un robot')",
    "button:has-text('Verify')",
    "button:has-text('Verificar')",
    "button:has-text('Continue')",
    "button:has-text('Continuar')",
    "#challenge-form button[type='submit']",
    "#challenge-stage input[type='submit']",
)

CAPTCHA_FRAMES = (
    "iframe[src*='recaptcha']",
    "iframe[src*='turnstile']",
    "iframe[src*='hcaptcha']",
    "iframe[title*='reCAPTCHA']",
    "iframe[title*='Cloudflare']",
)

CAPTCHA_INNER_SELECTORS = (
    "#recaptcha-anchor",
    "input[type='checkbox']",
    "div[role='checkbox']",
    ".cf-turnstile",
    "input[type='submit']",
)


# Acelera timers de espera de las webs de descarga. Se inyecta sólo en el flujo
# multi-click, nunca al resolver un challenge anti-bot (podría romperlo).
TIMER_EVASION_SCRIPT = """
(() => {
  const MAX_DELAY = 50;
  const realSetTimeout = window.setTimeout.bind(window);
  const realSetInterval = window.setInterval.bind(window);
  window.setTimeout = function (fn, delay, ...args) {
    let ms = typeof delay === "number" ? Math.min(delay, MAX_DELAY) : delay;
    return realSetTimeout(fn, ms, ...args);
  };
  window.setInterval = function (fn, delay, ...args) {
    let ms = typeof delay === "number" ? Math.min(delay, MAX_DELAY) : delay;
    return realSetInterval(fn, ms, ...args);
  };
  const realNow = Date.now.bind(Date);
  const started = realNow();
  Date.now = () => started + (realNow() - started) * 20;
  if (window.performance && performance.now) {
    const realPerfNow = performance.now.bind(performance);
    performance.now = () => realPerfNow() * 20;
  }
})();
"""

# JS usado por EXTRACT_URL: prioriza enlaces de descarga, data-* y formularios.
EXTRACT_URL_JS = """
(selector) => {
  const clean = (value) => {
    if (!value) return "";
    try { return new URL(value, document.baseURI).href; } catch (e) { return value; }
  };
  const pick = (el) =>
    clean(
      el.getAttribute("href") ||
      el.getAttribute("data-href") ||
      el.getAttribute("data-url") ||
      el.getAttribute("data-clipboard-text") ||
      el.getAttribute("formaction") ||
      el.getAttribute("src") ||
      ""
    );
  if (selector) {
    for (const el of document.querySelectorAll(selector)) {
      const url = pick(el);
      if (url) return { url, form: false };
    }
  }
  const anchors = Array.from(document.querySelectorAll("a[download], a[href]"));
  const score = (el) => {
    const text = ((el.id || "") + " " + (el.className || "") + " " +
                  (el.textContent || "")).toLowerCase();
    let value = 0;
    ["download", "descargar", "getlink", "get link", "continuar",
     "continue", "archivo", "file"].forEach((word) => {
      if (text.includes(word)) value += 1;
    });
    if (el.hasAttribute("download")) value += 4;
    if (/\\.(zip|rar|7z|pdf|apk|exe|mp4|mkv|mp3|docx?|xlsx?|epub)(\\?|$)/i
        .test(el.href || "")) value += 3;
    return value;
  };
  const best = anchors.sort((a, b) => score(b) - score(a))[0];
  if (best && score(best) > 0) return { url: pick(best), form: false };
  const form = document.querySelector("form[action]");
  if (form) return { url: clean(form.getAttribute("action")), form: true };
  return { url: "", form: false };
}
"""


@dataclass
class BrowserAction:
    """Una acción del flujo multi-click."""

    kind: str
    selector: str | None = None
    text: str | None = None
    seconds: float | None = None
    timeout: float | None = None
    keep_popup: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BrowserAction":
        return cls(
            kind=str(data.get("kind") or data.get("action") or "").upper(),
            selector=data.get("selector"),
            text=data.get("text"),
            seconds=data.get("seconds"),
            timeout=data.get("timeout"),
            keep_popup=bool(data.get("keep_popup")),
        )


def coerce_actions(actions: Sequence[Any] | None) -> list[BrowserAction]:
    """Normaliza dicts o BrowserAction en una lista manejable."""
    result: list[BrowserAction] = []
    for item in actions or []:
        if isinstance(item, BrowserAction):
            result.append(item)
        elif isinstance(item, dict):
            result.append(BrowserAction.from_dict(item))
    return result


def is_bot_challenge(status: int = 200, html: str = "", url: str | None = None) -> bool:
    """¿La respuesta es una pantalla anti-bot (y no el destino final)?"""
    text = (html or "").lower()
    if any(marker in text for marker in BOT_CHALLENGE_MARKERS):
        return True
    if status in (403, 503) and any(
        marker in text
        for marker in ("cloudflare", "captcha", "challenge", "access denied")
    ):
        return True
    if url:
        low_url = url.lower()
        if any(fragment in low_url for fragment in ("/cdn-cgi/challenge", "captcha")):
            return True
    return False


@dataclass
class BrowserOutcome:
    """Resultado de una resolución con navegador."""

    final_url: str
    filename: str = ""
    destination: Path | None = None
    html: str = ""
    cookies: list[dict[str, Any]] = field(default_factory=list)
    download_url: str | None = None
    extracted_url: str | None = None
    timed_out: bool = False


class BrowserResolver:
    """Resuelve challenges con Playwright.

    ``playwright_factory`` permite inyectar un doble en los tests sin depender
    de que Chromium esté instalado.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_wait: float = DEFAULT_MAX_WAIT,
        verbose: bool = False,
        user_agent: str | None = None,
        headless: bool = True,
        playwright_factory: Callable[[], Any] | None = None,
        cookies: Any = None,
        poll_interval: float = 0.5,
        allow_private: bool = False,
        max_sequence_seconds: float = 120.0,
    ) -> None:
        self.timeout = timeout
        self.max_wait = max_wait
        self.max_sequence_seconds = max(1.0, max_sequence_seconds)
        self.verbose = verbose
        self.user_agent = user_agent or DEFAULT_UA
        self.headless = headless
        self.playwright_factory = playwright_factory
        self.cookies = cookies
        self.poll_interval = max(0.1, poll_interval)
        self.allow_private = allow_private

    # -- helpers ---------------------------------------------------------
    def _log(self, message: str) -> None:
        if not self.verbose:
            return
        print(f"[browser] {message}", file=sys.stderr, flush=True)

    @staticmethod
    def available() -> bool:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False
        return True

    def _factory(self) -> Callable[[], Any]:
        if self.playwright_factory is not None:
            return self.playwright_factory
        from playwright.sync_api import sync_playwright

        return sync_playwright

    @staticmethod
    def _cookies_for_context(jar: Any) -> list[dict[str, Any]]:
        cookies: list[dict[str, Any]] = []
        if jar is None:
            return cookies
        try:
            iterator = list(jar)
        except TypeError:
            return cookies
        for cookie in iterator:
            domain = getattr(cookie, "domain", None)
            name = getattr(cookie, "name", None)
            value = getattr(cookie, "value", None)
            if not domain or not name:
                continue
            cookies.append(
                {
                    "name": name,
                    "value": value or "",
                    "domain": domain,
                    "path": getattr(cookie, "path", "/") or "/",
                }
            )
        return cookies

    def _guard_route(self, route: Any) -> None:
        """Bloquea peticiones del navegador a la red interna (SSRF)."""
        try:
            request_url = route.request.url
            scheme = urlsplit(request_url).scheme.lower()
            if scheme not in ("http", "https"):
                route.continue_()
                return
            validate_url(request_url, allow_private=self.allow_private)
        except UnsafeUrlError as exc:
            self._log(f"petición bloqueada por SSRF: {exc}")
            route.abort()
            return
        except Exception:
            route.continue_()
            return
        route.continue_()

    def _page_has_challenge(self, page: Any) -> bool:
        try:
            title = page.title() or ""
        except Exception:
            title = ""
        try:
            content = page.content() or ""
        except Exception:
            content = ""
        if is_bot_challenge(200, f"{title}\n{content}"):
            return True
        for selector in CHALLENGE_SELECTORS:
            try:
                if page.locator(selector).first.is_visible(timeout=200):
                    return True
            except Exception:
                continue
        return False

    def _click_verification(self, page: Any) -> bool:
        """Intenta pulsar "No soy un robot" o el checkbox del captcha."""
        for selector in CLICK_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.is_visible(timeout=300):
                    locator.click(timeout=2000)
                    self._log(f"clic en {selector}")
                    return True
            except Exception:
                continue

        for frame_selector in CAPTCHA_FRAMES:
            try:
                frame = page.frame_locator(frame_selector)
            except Exception:
                continue
            for inner in CAPTCHA_INNER_SELECTORS:
                try:
                    locator = frame.locator(inner).first
                    if locator.is_visible(timeout=300):
                        locator.click(timeout=2000)
                        self._log(f"clic en {frame_selector} -> {inner}")
                        return True
                except Exception:
                    continue
        return False

    def _wait(self, page: Any) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        try:
            page.wait_for_timeout(int(self.poll_interval * 1000))
        except Exception:
            time.sleep(self.poll_interval)

    def _close_extra_tabs(self, extra_pages: list[Any], main_page: Any) -> int:
        """Cierra pop-ups/publicidad y devuelve el foco a la pestaña original."""
        closed = 0
        for candidate in list(extra_pages):
            try:
                extra_pages.remove(candidate)
            except ValueError:
                pass
            if candidate is main_page:
                continue
            try:
                candidate.close()
                closed += 1
            except Exception:
                continue
        if closed:
            self._log(f"cerradas {closed} pestaña(s) emergente(s)")
        try:
            main_page.bring_to_front()
        except Exception:
            pass
        return closed

    def _click(self, page: Any, action: BrowserAction, deadline: float) -> bool:
        remaining = max(0.2, deadline - time.monotonic())
        timeout_ms = int(min(action.timeout or 3.0, remaining) * 1000)
        try:
            if action.selector:
                locator = page.locator(action.selector).first
            elif action.text:
                locator = page.get_by_text(action.text, exact=False).first
            else:
                return False
            if not locator.is_visible(timeout=min(1000, timeout_ms)):
                return False
            locator.click(timeout=timeout_ms)
            self._log(f"clic en {action.selector or action.text}")
            return True
        except Exception as exc:
            self._log(f"clic falló ({action.selector or action.text}): {exc}")
            return False

    def _wait_action(
        self, page: Any, action: BrowserAction, deadline: float
    ) -> bool:
        """Devuelve False si la espera consumió todo el presupuesto restante."""
        remaining = max(0.0, deadline - time.monotonic())
        if action.selector:
            requested = action.timeout if action.timeout is not None else remaining
            effective = min(requested, remaining)
            try:
                page.wait_for_selector(
                    action.selector, timeout=int(effective * 1000)
                )
                return True
            except Exception:
                self._log(f"timeout esperando {action.selector}")
                return effective < requested
        requested = action.seconds
        if requested is None:
            requested = self.poll_interval
        requested = max(0.0, float(requested))
        effective = min(requested, remaining)
        try:
            page.wait_for_timeout(int(effective * 1000))
        except Exception:
            time.sleep(effective)
        return effective >= requested and remaining > requested

    def _extract_url(self, page: Any, selector: str | None = None) -> str | None:
        """Extrae del DOM el enlace de descarga; si hay un formulario, lo envía."""
        try:
            result = page.evaluate(EXTRACT_URL_JS, selector or "")
        except Exception as exc:
            self._log(f"extracción falló: {exc}")
            return None
        if not isinstance(result, dict):
            return None
        if result.get("form"):
            self._log("formulario de descarga detectado; enviando…")
            try:
                page.evaluate(
                    "() => { const f = document.querySelector('form[action]');"
                    " if (f) f.requestSubmit(); }"
                )
            except Exception:
                pass
            self._wait(page)
            return None
        url = str(result.get("url") or "").strip()
        if not url:
            return None
        current = getattr(page, "url", "") or ""
        url = urljoin(current, url)
        if not url.startswith(("http://", "https://")):
            return None
        if not is_plausible_url(url):
            self._log(f"URL descartada (parece un recurso): {url}")
            return None
        return url

    def _run_sequence(
        self,
        page: Any,
        extra_pages: list[Any],
        actions: list[BrowserAction],
        outcome: BrowserOutcome,
        download_holder: dict[str, Any],
    ) -> None:
        """Ejecuta el flujo multi-click con un deadline estricto."""
        deadline = time.monotonic() + min(self.max_wait, self.max_sequence_seconds)
        for index, action in enumerate(actions, 1):
            if download_holder.get("download") is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome.timed_out = True
                self._log("flujo multi-click agotado (timeout estricto)")
                break

            if self._page_has_challenge(page):
                self._click_verification(page)
                self._wait(page)
                if self._page_has_challenge(page):
                    continue

            kind = action.kind.upper()
            self._log(f"paso {index}/{len(actions)}: {kind}")
            if kind == "CLOSE_POPUP":
                self._close_extra_tabs(extra_pages, page)
            elif kind == "WAIT":
                if not self._wait_action(page, action, deadline):
                    outcome.timed_out = True
                    self._log("flujo multi-click agotado durante una espera")
                    break
            elif kind == "CLICK":
                if self._click(page, action, deadline):
                    self._wait(page)
                    if not action.keep_popup:
                        self._close_extra_tabs(extra_pages, page)
            elif kind == "EXTRACT_URL":
                url = self._extract_url(page, action.selector)
                if url:
                    outcome.extracted_url = url
                    self._log(f"URL extraída: {url}")
                    break
            else:
                self._log(f"acción desconocida: {action.kind}")

        has_extract_step = any(
            action.kind.upper() == "EXTRACT_URL" for action in actions
        )
        if (
            download_holder.get("download") is None
            and not outcome.extracted_url
            and not has_extract_step
            and time.monotonic() < deadline
        ):
            url = self._extract_url(page)
            if url:
                outcome.extracted_url = url
                self._log(f"URL extraída (paso final): {url}")
        if time.monotonic() >= deadline:
            outcome.timed_out = True

    # -- resolución ------------------------------------------------------
    def resolve(
        self,
        url: str,
        *,
        save: bool = False,
        output: str | None = None,
        force: bool = False,
        as_dir: bool = True,
        actions: Sequence[Any] | None = None,
        speed_up_timers: bool = False,
    ) -> BrowserOutcome:
        outcome = BrowserOutcome(final_url=url)
        download_holder: dict[str, Any] = {}
        plan = coerce_actions(actions)

        def on_download(download: Any) -> None:
            download_holder.setdefault("download", download)

        try:
            with self._factory()() as playwright:
                browser = playwright.chromium.launch(headless=self.headless)
                context = browser.new_context(
                    user_agent=self.user_agent,
                    viewport={"width": 1366, "height": 768},
                    accept_downloads=True,
                )
                initial_cookies = self._cookies_for_context(self.cookies)
                if initial_cookies:
                    try:
                        context.add_cookies(initial_cookies)
                    except Exception:
                        pass

                page = context.new_page()
                extra_pages: list[Any] = []

                def on_new_page(new_page: Any) -> None:
                    if new_page is page:
                        return
                    extra_pages.append(new_page)
                    try:
                        new_page.on("download", on_download)
                    except Exception:
                        pass
                    if hasattr(new_page, "route"):
                        try:
                            new_page.route("**/*", self._guard_route)
                        except Exception:
                            pass

                try:
                    context.on("page", on_new_page)
                except Exception:
                    pass
                page.on("download", on_download)
                if hasattr(page, "route"):
                    try:
                        page.route("**/*", self._guard_route)
                    except Exception:
                        pass
                if speed_up_timers:
                    try:
                        page.add_init_script(TIMER_EVASION_SCRIPT)
                    except Exception:
                        pass
                try:
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=int(self.timeout * 1000),
                    )
                except Exception as exc:
                    self._log(f"goto falló: {exc}")

                if plan:
                    self._run_sequence(
                        page, extra_pages, plan, outcome, download_holder
                    )
                else:
                    deadline = time.monotonic() + self.max_wait
                    while time.monotonic() < deadline:
                        if download_holder.get("download") is not None:
                            break
                        current = getattr(page, "url", "") or ""
                        if current:
                            outcome.final_url = current
                        if not self._page_has_challenge(page):
                            break
                        self._click_verification(page)
                        self._wait(page)

                current = getattr(page, "url", "") or ""
                if current:
                    outcome.final_url = current

                try:
                    outcome.cookies = list(context.cookies())
                except Exception:
                    outcome.cookies = []

                download = download_holder.get("download")
                if download is not None:
                    download_url = getattr(download, "url", "") or outcome.final_url
                    outcome.download_url = download_url
                    outcome.final_url = download_url
                    suggested = getattr(download, "suggested_filename", "") or ""
                    outcome.filename = sanitize_filename(
                        ensure_extension(
                            suggested or filename_from_url(download_url) or "download.bin",
                            None,
                        )
                    )
                    if save:
                        destination = reserve_destination(
                            output, outcome.filename, as_dir=as_dir, force=force
                        )
                        part = Path(str(destination) + ".part")
                        try:
                            download.save_as(str(part))
                            part.replace(destination)
                        except Exception:
                            part.unlink(missing_ok=True)
                            release_destination(destination)
                            raise
                        outcome.destination = destination
                else:
                    try:
                        outcome.html = page.content() or ""
                    except Exception:
                        outcome.html = ""
                    if outcome.extracted_url:
                        outcome.final_url = outcome.extracted_url
                    outcome.filename = sanitize_filename(
                        filename_from_url(outcome.final_url) or "download.bin"
                    )

                try:
                    browser.close()
                except Exception:
                    pass
        except Exception as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise RuntimeError(f"Fallo del navegador headless: {exc}") from exc

        return outcome


def browser_resolve_and_download(
    start_url: str,
    output: str | None,
    *,
    force: bool = False,
    as_dir: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    max_wait: float = DEFAULT_MAX_WAIT,
    verbose: bool = False,
    save: bool = True,
    user_agent: str | None = None,
    resolver: Any = None,
    allow_private: bool = False,
    actions: Sequence[Any] | None = None,
    speed_up_timers: bool = False,
    max_sequence_seconds: float = 120.0,
) -> tuple[str, str, Path | None]:
    """Compatibilidad: resuelve y opcionalmente descarga con el navegador.

    Devuelve (url_final, nombre_archivo, ruta_destino). Si se pasa ``resolver``,
    se importan en su sesión HTTP las cookies obtenidas por el navegador.
    """
    cookies = getattr(getattr(resolver, "session", None), "cookies", None)
    browser = BrowserResolver(
        timeout=timeout,
        max_wait=max_wait,
        verbose=verbose,
        user_agent=user_agent,
        cookies=cookies,
        allow_private=allow_private,
        max_sequence_seconds=max_sequence_seconds,
    )
    outcome = browser.resolve(
        start_url,
        save=save,
        output=output,
        force=force,
        as_dir=as_dir,
        actions=actions,
        speed_up_timers=speed_up_timers,
    )

    if resolver is not None and outcome.cookies:
        try:
            for cookie in outcome.cookies:
                resolver.session.cookies.set(
                    cookie.get("name", ""),
                    cookie.get("value", ""),
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
        except Exception:
            pass

    return outcome.final_url, outcome.filename, outcome.destination
