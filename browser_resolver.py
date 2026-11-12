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
from typing import Any, Callable

from urllib.parse import urlsplit

from desenlace_core import (
    DEFAULT_MAX_WAIT,
    DEFAULT_TIMEOUT,
    DEFAULT_UA,
    UnsafeUrlError,
    ensure_extension,
    filename_from_url,
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
    ) -> None:
        self.timeout = timeout
        self.max_wait = max_wait
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

    # -- resolución ------------------------------------------------------
    def resolve(
        self,
        url: str,
        *,
        save: bool = False,
        output: str | None = None,
        force: bool = False,
        as_dir: bool = True,
    ) -> BrowserOutcome:
        outcome = BrowserOutcome(final_url=url)
        download_holder: dict[str, Any] = {}

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
                page.on("download", on_download)
                if hasattr(page, "route"):
                    try:
                        page.route("**/*", self._guard_route)
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
    )
    outcome = browser.resolve(
        start_url, save=save, output=output, force=force, as_dir=as_dir
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
