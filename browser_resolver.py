"""Capa anti-bot: resuelve desafíos con un navegador headless (Playwright).

Se activa sólo cuando el resolver HTTP detecta un challenge (Cloudflare,
Turnstile, reCAPTCHA, "Checking your browser", "I am not a robot"...). El
módulo es independiente del motor: recibe una URL y devuelve la URL final,
las cookies de la sesión y, si hubo descarga, el archivo guardado.
"""

from __future__ import annotations

import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urljoin, urlsplit

try:
    from playwright_stealth import Stealth

    def _apply_stealth(page):
        try:
            Stealth().apply_stealth_sync(page)
        except Exception:
            pass
except ImportError:
    def _apply_stealth(page):
        pass

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

# Cabeceras para que las webs no detecten Chromium headless y devuelvan HTML
# vacío. `sec-ch-ua` es la que delata "HeadlessChrome". También se evita zstd,
# que algunas compilaciones de Chromium no decodifican y dejan la página vacía.
HEADLESS_MASK_HEADERS = {
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "Accept-Encoding": "gzip, deflate, br",
}

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

BUTTON_KEYWORDS = (
    "descarga directa",
    "descargar",
    "download",
    "continuar",
    "continue",
    "ir a la descarga",
    "go to download",
    "get link",
    "obtener enlace",
    "generar enlace",
    "saltar",
    "skip",
    "no soy un robot",
    "verificar",
    "verify",
    "acceder",
    "abrir enlace",
    "click aquí",
    "clic aquí",
    "click here",
    "proceed",
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
     "continue", "next", "siguiente", "paso", "step", "archivo", "file"
    ].forEach((word) => {
      if (text.includes(word)) value += 1;
    });
    if (el.hasAttribute("download")) value += 4;
    if (/\\.(txt|zip|rar|7z|apk|exe)(\\?|$)/i.test(el.href || "")) value += 6;
    if (/\\.(pdf|docx?|xlsx?|epub)(\\?|$)/i.test(el.href || "")) value += 1;
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
        maze: bool = False,
        debug: bool = False,
        slow_mo: int | None = None,
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
        self.maze = maze
        self.debug = debug
        self.slow_mo = slow_mo

    # -- helpers ---------------------------------------------------------
    def _log(self, message: str) -> None:
        if not (self.verbose or self.debug):
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
            try:
                locator.click(timeout=timeout_ms)
            except Exception:
                try:
                    # Overlays/publicidad a veces interceptan el clic.
                    locator.click(timeout=timeout_ms, force=True)
                except Exception:
                    # Último recurso: click() nativo del DOM (burbujea igual).
                    locator.evaluate("el => el.click()")
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
            try:
                action = page.evaluate(
                    "() => document.querySelector('form[action]')?.getAttribute('action') || ''"
                )
            except Exception:
                action = ""
            # Los formularios de WordPress (comentarios) son un falso positivo:
            # enviarlos no lleva al archivo y rompe el flujo del anuncio.
            if "wp-" in (action or "").lower():
                self._log(f"formulario WP ignorado: {action}")
                return None
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
        if "wp-" in url.lower():
            self._log(f"URL WP descartada: {url}")
            return None
        return url

    def _page_text(self, page: Any) -> str:
        try:
            return page.inner_text("body") or ""
        except Exception:
            try:
                return page.content() or ""
            except Exception:
                return ""

    def _close_popups(self, context: Any, page: Any) -> int:
        """Cierra TODAS las pestañas extra (publicidad, popups) y devuelve el foco a `page`.

        En el contexto del laberinto de descarga, cualquier pestaña que no sea
        la principal es basura. No filtramos por dominio: las cerramos todas.
        """
        closed = 0
        try:
            pages = list(context.pages)
        except Exception:
            return 0
        for candidate in pages:
            if candidate is page:
                continue
            try:
                url = ""
                try:
                    url = candidate.url or ""
                except Exception:
                    pass
                self._log(f"[POPUP] Cerrando pestaña extra: {url[:80]}")
                candidate.close()
                closed += 1
            except Exception:
                pass
        if closed:
            self._log(f"[POPUPS] Cerradas {closed} pestaña(s) extra")
        try:
            page.bring_to_front()
        except Exception:
            pass
        return closed

    def _find_text_button(
        self, page: Any, exclude: set[str] | None = None
    ) -> Any | None:
        """Busca un botón/enlace visible por su texto, sin depender de id/clase."""
        exclude = exclude or set()
        
        # Buscar PRIMERO por ID conocidos
        try:
            main_btn = page.locator("#mainButton")
            if main_btn.count() > 0:
                try:
                    if main_btn.first.is_visible(timeout=500):
                        self._log(f"Botón encontrado por ID: #mainButton")
                        return main_btn.first
                except:
                    pass
        except:
            pass
        
        candidatos = (
            "a, button, input[type=submit], input[type=button], [role=button], div[onclick]"
        )
        try:
            items = page.locator(candidatos)
            total = items.count()
            for index in range(min(total, 200)):
                item = items.nth(index)
                try:
                    if not item.is_visible(timeout=200):
                        continue
                except Exception:
                    continue
                # Descartar elementos ocultos de accesibilidad (screen-reader-text)
                try:
                    cls = (item.get_attribute("class") or "").lower()
                    if "screen-reader-text" in cls:
                        continue
                except Exception:
                    pass
                texto = ""
                try:
                    texto = item.inner_text() or ""
                except Exception:
                    texto = ""
                if not texto:
                    try:
                        texto = item.get_attribute("value") or ""
                    except Exception:
                        texto = ""
                if not texto:
                    try:
                        texto = item.get_attribute("aria-label") or ""
                    except Exception:
                        texto = ""
                
                # Si aún no hay texto, intentar obtener HTML interno
                if not texto:
                    try:
                        html = item.inner_html()
                        if "download" in html.lower() or "descarga" in html.lower():
                            texto = html[:100]
                    except:
                        pass
                
                low = re.sub(r"\s+", " ", texto.strip().lower())
                if not low or low in exclude:
                    continue
                if any(keyword in low for keyword in BUTTON_KEYWORDS):
                    self._log(f"Botón por texto: {texto.strip()[:60]!r}")
                    return item
        except Exception:
            pass
        # Fallback para dobles de test o páginas sin locator() completo.
        for keyword in BUTTON_KEYWORDS:
            if keyword in exclude:
                continue
            for texto in (keyword, keyword.capitalize()):
                try:
                    loc = page.get_by_text(texto, exact=False).first
                    if not loc.is_visible(timeout=200):
                        continue
                    # Descartar elementos ocultos de accesibilidad
                    try:
                        cls = (loc.get_attribute("class") or "").lower()
                        if "screen-reader-text" in cls:
                            continue
                    except Exception:
                        pass
                    self._log(f"Botón por texto (get_by_text): {texto!r}")
                    return loc
                except Exception:
                    continue
        return None

    def _remove_blocking_overlays(self, page: Any) -> None:
        """Elimina AGRESIVAMENTE overlays, iframes y publicidad que bloquean clics.

        Además de divs con z-index > 50, también elimina overlays transparentes
        (pointer-events: none con dimensiones de pantalla completa) que actúan
        como trampas de clic invisibles.
        """
        try:
            removed = page.evaluate("""() => {
                let count = 0;
                // Pase 1: overlays con z-index alto o position:fixed que cubren gran parte de la pantalla
                try {
                    document.querySelectorAll('*').forEach(el => {
                        const style = window.getComputedStyle(el);
                        const z = parseInt(style.zIndex) || 0;
                        const p = style.position;
                        if ((z > 50 || p === 'fixed') && el.offsetHeight > 0 && el.offsetWidth > 0) {
                            const r = el.getBoundingClientRect();
                            if (r.width > window.innerWidth * 0.3 && r.height > window.innerHeight * 0.2) {
                                try { el.remove(); count++; } catch(e) {}
                            }
                        }
                    });
                } catch(e) {}
                // Pase 2: overlays transparentes con pointer-events que interceptan clics
                try {
                    document.querySelectorAll('div, section, aside').forEach(el => {
                        const style = window.getComputedStyle(el);
                        const z = parseInt(style.zIndex) || 0;
                        const pe = style.pointerEvents;
                        const bg = style.backgroundColor || '';
                        const isTransparent = bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent' || style.opacity === '0';
                        if (z > 10 && pe !== 'none' && isTransparent &&
                            el.offsetWidth > window.innerWidth * 0.5 &&
                            el.offsetHeight > window.innerHeight * 0.4) {
                            try { el.remove(); count++; } catch(e) {}
                        }
                    });
                } catch(e) {}
                // Pase 3: iframes de publicidad
                try {
                    document.querySelectorAll('iframe').forEach(el => {
                        const s = (el.src || '').toLowerCase();
                        const i = (el.id || '').toLowerCase();
                        const c = (el.className || '').toLowerCase();
                        if (s.includes('ad') || s.includes('banner') || s.includes('pop') ||
                            i.includes('ad') || i.includes('popup') || i.includes('banner') ||
                            c.includes('ad') || c.includes('popup')) {
                            try { el.remove(); count++; } catch(e) {}
                        }
                    });
                } catch(e) {}
                // Pase 4: elementos con clases de publicidad/modales
                try {
                    document.querySelectorAll('[class*="ad"], [class*="popup"], [class*="modal"], [class*="overlay"], [class*="banner"]').forEach(el => {
                        if (el.offsetHeight > 30) try { el.remove(); count++; } catch(e) {}
                    });
                } catch(e) {}
                return count;
            }""")
            if removed > 0:
                self._log(f"[AD-KILLER] Removidos {removed} overlays")
        except Exception as e:
            # Context destroyed es normal durante navegación - no loguear
            if "context" not in str(e).lower():
                self._log(f"[AD-KILLER] Error: {str(e)[:50]}")

    def _click_locator(self, page: Any, locator: Any) -> bool:
        self._remove_blocking_overlays(page)
        try:
            if not locator.is_visible(timeout=500):
                return False
        except Exception:
            pass
        
        # Estrategia de clic: scroll -> normal -> force -> js directo
        for accion in ("scroll", "normal", "force", "js"):
            try:
                if accion == "scroll":
                    # Scroll + pausa antes de clic
                    try:
                        locator.scroll_into_view_if_needed()
                    except:
                        pass
                    time.sleep(0.3)
                    locator.click(timeout=3000)
                elif accion == "normal":
                    locator.click(timeout=3000)
                elif accion == "force":
                    locator.click(timeout=3000, force=True)
                else:  # js
                    # Clic JavaScript puro
                    page.evaluate("""() => {
                        const el = document.querySelector('[role=button], button, a');
                        if (el) el.click();
                    }""")
                return True
            except Exception as e:
                err_str = str(e)
                # Contexto destruido es normal, no loguear
                if "context" not in err_str.lower():
                    self._log(f"Clic {accion} falló: {err_str[:30]}")
                continue
        return False

    def _find_bait_button(self, page: Any) -> Any | None:
        """Busca botón cebo: Continuar, Siguiente, botones verdes, etc (NO Descarga Directa).

        Estrategia de detección por orden de prioridad:
        0. Botones con IDs conocidos de wrappers (#mainButton, #downloadButton, etc.)
        1. Botones con keywords de cebo (continuar, siguiente, etc.)
        2. Botones/links con color de fondo verde (#28a745, #5cb85c, etc.)
        3. Botones grandes y centrados sin texto de descarga
        """
        # Prioridad 0: IDs conocidos de wrappers/pasarelas que actúan como cebo
        known_bait_ids = ('mainbutton', 'downloadbutton', 'continuebtn',
                          'nextbtn', 'skipbtn', 'proceedbtn', 'waiting-btn')
        for bait_id in known_bait_ids:
            try:
                loc = page.locator(f"#{bait_id}")
                if loc.count() > 0:
                    try:
                        if loc.first.is_visible(timeout=500):
                            self._log(
                                f"[BAIT-DETECT] Botón cebo por ID: #{bait_id}"
                            )
                            return loc.first
                    except Exception:
                        pass
            except Exception:
                pass

        bait_keywords = [
            'continuar', 'continue', 'siguiente', 'next', 'pulsa',
            'click', 'acceso', 'ver', 'go', 'proceed', 'abrir',
            'empezar', 'start', 'comenzar', 'ir', 'entrar',
        ]
        exclude_keywords = [
            'descarga directa', 'direct download', 'download',
            'descargar archivo', 'enlace directo', 'get link',
            '.txt', '.zip', '.rar',
        ]
        # Necesitamos window.innerWidth para el cálculo de posición centrada
        try:
            win_w = page.evaluate("() => window.innerWidth") or 1366
        except Exception:
            win_w = 1366

        try:
            items = page.locator(
                'a, button, input[type=submit], input[type=button], '
                '[role=button], div[onclick]'
            )
            best_candidate = None
            best_score = 0

            for idx in range(min(items.count(), 150)):
                try:
                    item = items.nth(idx)
                    if not item.is_visible(timeout=200):
                        continue
                except Exception:
                    continue

                texto = ""
                try:
                    texto = (item.inner_text() or "").strip()
                except Exception:
                    pass
                if not texto:
                    try:
                        texto = (item.get_attribute("value") or "").strip()
                    except Exception:
                        pass
                if not texto:
                    try:
                        texto = (item.get_attribute("aria-label") or "").strip()
                    except Exception:
                        pass

                low = texto.lower()

                # Descartar si es un botón de descarga real
                if any(kw in low for kw in exclude_keywords):
                    continue

                score = 0

                # Puntuación por keyword de cebo
                for kw in bait_keywords:
                    if kw in low:
                        score += 3 if kw in ('continuar', 'continue',
                                             'siguiente', 'next') else 1

                # Detección por color verde (muy común en botones cebo)
                try:
                    bg = item.evaluate(
                        "el => window.getComputedStyle(el).backgroundColor"
                    )
                    if bg and (
                        'rgb(40, 167, 69)' in bg or    # #28a745 Bootstrap success
                        'rgb(92, 184, 92)' in bg or     # #5cb85c
                        'rgb(76, 175, 80)' in bg or     # #4caf50 Material green
                        'rgb(0, 128, 0)' in bg or       # green
                        'rgb(34, 139, 34)' in bg or     # forestgreen
                        'rgb(60, 179, 113)' in bg       # mediumseagreen
                    ):
                        score += 4
                        self._log(
                            f"[BAIT-DETECT] Botón verde: '{texto[:40]}' bg={bg}"
                        )
                except Exception:
                    pass

                # Puntuación por posición (centrado, grande)
                try:
                    rect = item.evaluate("""el => {
                        const r = el.getBoundingClientRect();
                        return {w: r.width, h: r.height, cx: r.x + r.width / 2};
                    }""")
                    if (rect['w'] > 120 and rect['h'] > 30 and
                            abs(rect['cx'] - win_w / 2) < 200):
                        score += 1
                except Exception:
                    pass

                if score > best_score:
                    best_score = score
                    best_candidate = item

            if best_candidate and best_score >= 3:
                try:
                    t = best_candidate.inner_text(timeout=300).strip()[:50]
                except Exception:
                    t = "?"
                self._log(
                    f"[BAIT-DETECT] Botón cebo (score={best_score}): '{t}'"
                )
                return best_candidate

        except Exception:
            pass

        # Fallback con get_by_text para compatibilidad con tests
        for keyword in bait_keywords:
            try:
                loc = page.get_by_text(keyword, exact=False).first
                if loc.is_visible(timeout=200):
                    self._log(
                        f"[BAIT-DETECT] Botón cebo (get_by_text): '{keyword}'"
                    )
                    return loc
            except Exception:
                continue
        return None
    
    def _find_real_download_button(self, page: Any) -> Any | None:
        """Busca botón REAL de descarga: enlaces .txt, 'Descarga Directa', etc.

        Prioridades:
        1. Enlaces <a href="...txt"> o <a download>
        2. Botones con texto 'Descarga Directa', 'Download', etc.
        3. Cualquier botón/enlace grande y visible con aspecto de descarga
        """
        dl_keywords = [
            'descarga directa', 'direct download', 'download',
            'descargar archivo', 'enlace directo', 'direct link',
            'obtener archivo', 'get file', 'descargar', 'bajar',
        ]

        # Prioridad 1: Enlaces directos .txt o con atributo download
        try:
            links = page.locator('a[href*=".txt"], a[download]')
            for i in range(min(links.count(), 10)):
                try:
                    link = links.nth(i)
                    if link.is_visible(timeout=300):
                        href = ""
                        try:
                            href = link.get_attribute("href") or ""
                        except Exception:
                            pass
                        self._log(
                            f"[REAL-DL] Enlace directo .txt/download: {href[:80]}"
                        )
                        return link
                except Exception:
                    pass
        except Exception:
            pass

        # Prioridad 2: Buscar por keywords de descarga
        try:
            items = page.locator(
                'a, button, input[type=submit], input[type=button], '
                '[role=button], div[onclick]'
            )
            best = None
            best_score = 0

            for idx in range(min(items.count(), 150)):
                try:
                    item = items.nth(idx)
                    if not item.is_visible(timeout=200):
                        continue
                except Exception:
                    continue

                texto = ""
                try:
                    texto = (item.inner_text() or "").strip()
                except Exception:
                    pass
                if not texto:
                    try:
                        texto = (item.get_attribute("value") or "").strip()
                    except Exception:
                        pass
                if not texto:
                    try:
                        texto = (item.get_attribute("aria-label") or "").strip()
                    except Exception:
                        pass

                low = texto.lower()
                score = 0

                for kw in dl_keywords:
                    if kw in low:
                        score += 4 if 'directa' in kw or 'direct' in kw else 2

                # Bonus si tiene href con extensión de archivo
                try:
                    href = (item.get_attribute("href") or "").lower()
                    if any(ext in href for ext in
                           ('.txt', '.zip', '.rar', '.7z', '.pdf', '.apk')):
                        score += 5
                except Exception:
                    pass

                # Bonus por posición/visibilidad
                try:
                    rect = item.evaluate("""el => {
                        const r = el.getBoundingClientRect();
                        return {w: r.width, h: r.height, visible: r.width > 0};
                    }""")
                    if rect['w'] > 100 and rect['h'] > 25:
                        score += 1
                except Exception:
                    pass

                if score > best_score:
                    best_score = score
                    best = item

            if best and best_score >= 3:
                try:
                    t = best.inner_text(timeout=300).strip()[:50]
                except Exception:
                    t = "?"
                self._log(
                    f"[REAL-DL] Botón descarga (score={best_score}): '{t}'"
                )
                return best

        except Exception:
            pass

        # Prioridad 3: Fallback con get_by_text
        for kw in dl_keywords[:5]:
            try:
                loc = page.get_by_text(kw, exact=False).first
                if loc.is_visible(timeout=200):
                    self._log(f"[REAL-DL] get_by_text: '{kw}'")
                    return loc
            except Exception:
                continue

        return None
    
    def _bait_and_back_sequence(self, page: Any, context: Any,
                              outcome: "BrowserOutcome") -> bool:
        """Secuencia completa: cebo → popup → atrás → botón real → descarga.

        Flujo exacto que imita el comportamiento humano:
        1. Eliminar overlays antes de pulsar
        2. Buscar y pulsar el botón cebo (force=True)
        3. Esperar 2s a que se abra la pestaña de publicidad
        4. Cerrar TODAS las pestañas de publicidad
        5. page.go_back() para volver a la página anterior
        6. Esperar carga y eliminar overlays de nuevo
        7. Buscar botón REAL de descarga (hasta 3 reintentos)
        8. Pulsar botón real y verificar descarga
        """
        for attempt in range(1, 4):
            self._log(f"[BAIT-{attempt}/3] === INICIO SECUENCIA CEBO ===")

            # Paso 0: Limpiar overlays antes de buscar
            self._remove_blocking_overlays(page)
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)

            # Paso 1: Buscar botón cebo
            self._log(f"[BAIT-{attempt}] Buscando botón cebo...")
            bait = self._find_bait_button(page)
            if not bait:
                self._log(f"[BAIT-{attempt}] No se encontró botón cebo")
                return False

            try:
                bait_text = bait.inner_text(timeout=500).strip()[:60]
            except Exception:
                bait_text = "?"

            # Paso 2: Pulsar botón cebo con force=True
            self._log(f"[BAIT-{attempt}] Pulsando cebo: '{bait_text}'")
            url_antes = getattr(page, "url", "") or ""
            pages_antes = 0
            try:
                pages_antes = len(list(context.pages))
            except Exception:
                pass

            clicked = False
            for metodo in ("force", "js"):
                try:
                    if metodo == "force":
                        bait.click(timeout=3000, force=True)
                    else:
                        bait.evaluate("el => el.click()")
                    clicked = True
                    break
                except Exception as e:
                    self._log(
                        f"[BAIT-{attempt}] Clic {metodo} falló: {str(e)[:30]}"
                    )

            if not clicked:
                self._log(f"[BAIT-{attempt}] No se pudo pulsar el cebo")
                continue

            # Paso 3: Esperar 2 segundos a que se abra la publicidad
            self._log(f"[BAIT-{attempt}] Esperando 2s para popup...")
            try:
                page.wait_for_timeout(2000)
            except Exception:
                time.sleep(2)

            # Verificar si se abrieron pestañas nuevas
            pages_despues = 0
            try:
                pages_despues = len(list(context.pages))
            except Exception:
                pass
            self._log(
                f"[BAIT-{attempt}] Pestañas: {pages_antes} → {pages_despues}"
            )

            # Paso 4: Cerrar TODAS las pestañas de publicidad
            self._log(f"[BAIT-{attempt}] Cerrando popups...")
            self._close_popups(context, page)

            # Pequeña pausa para que los cierres se procesen
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)

            # Paso 5: page.go_back() — volver atrás
            self._log(f"[BAIT-{attempt}] Ejecutando page.go_back()...")
            try:
                page.go_back(wait_until='domcontentloaded', timeout=15000)
                self._log(f"[BAIT-{attempt}] go_back() OK")
            except Exception as e:
                self._log(f"[BAIT-{attempt}] go_back() falló: {str(e)[:50]}")
                # Intentar con timeout más largo
                try:
                    page.go_back(wait_until='load', timeout=20000)
                except Exception:
                    self._log(f"[BAIT-{attempt}] go_back() falló definitivamente")
                    continue

            # Paso 6: Esperar carga completa
            self._log(f"[BAIT-{attempt}] Esperando carga post-go_back...")
            try:
                page.wait_for_load_state('networkidle', timeout=15000)
            except Exception:
                pass
            try:
                page.wait_for_timeout(1500)
            except Exception:
                time.sleep(1.5)

            url_tras_back = getattr(page, "url", "") or ""
            self._log(
                f"[BAIT-{attempt}] URL tras go_back: {url_tras_back[:80]}"
            )

            # Limpiar overlays que puedan haber aparecido
            self._remove_blocking_overlays(page)

            # Paso 7: Buscar botón REAL con reintentos progresivos
            self._log(f"[BAIT-{attempt}] Buscando botón REAL de descarga...")
            real = None
            for retry in range(1, 4):
                real = self._find_real_download_button(page)
                if real:
                    break
                self._log(
                    f"[BAIT-{attempt}] Botón real no encontrado "
                    f"(reintento {retry}/3), esperando..."
                )
                try:
                    page.wait_for_timeout(2000)
                except Exception:
                    time.sleep(2)
                # Volver a limpiar overlays entre reintentos
                self._remove_blocking_overlays(page)

            if not real:
                self._log(
                    f"[BAIT-{attempt}] Botón real no encontrado "
                    f"tras 3 reintentos"
                )
                continue

            try:
                real_text = real.inner_text(timeout=500).strip()[:60]
            except Exception:
                real_text = "?"

            self._log(f"[BAIT-{attempt}] ✓ Botón real encontrado: '{real_text}'")

            # Paso 8: Pulsar botón real
            self._log(f"[BAIT-{attempt}] Pulsando botón real...")
            self._remove_blocking_overlays(page)

            clicked_real = False
            for metodo in ("force", "js", "normal"):
                try:
                    if metodo == "force":
                        real.click(timeout=5000, force=True)
                    elif metodo == "js":
                        real.evaluate("el => el.click()")
                    else:
                        real.click(timeout=5000)
                    clicked_real = True
                    break
                except Exception as e:
                    self._log(
                        f"[BAIT-{attempt}] Clic real ({metodo}) "
                        f"falló: {str(e)[:30]}"
                    )

            if not clicked_real:
                self._log(f"[BAIT-{attempt}] No se pudo pulsar el botón real")
                continue

            # Esperar navegación/descarga
            try:
                page.wait_for_timeout(3000)
            except Exception:
                time.sleep(3)

            try:
                page.wait_for_load_state('networkidle', timeout=15000)
            except Exception:
                pass

            # Verificar si se obtuvo descarga
            url_final = getattr(page, "url", "") or ""

            # Verificar vía interceptor (outcome.extracted_url)
            if outcome.extracted_url:
                self._log(
                    f"[BAIT-{attempt}] ✓✓✓ DESCARGA CAPTURADA: "
                    f"{outcome.extracted_url[:80]}"
                )
                return True

            # Verificar si la URL actual es un archivo
            if self._is_file_url(url_final):
                outcome.extracted_url = url_final
                self._log(
                    f"[BAIT-{attempt}] ✓✓✓ URL ARCHIVO: {url_final[:80]}"
                )
                return True

            # Buscar enlace .txt en el DOM
            txt_link = self._find_txt_link(page)
            if txt_link:
                outcome.extracted_url = txt_link
                self._log(
                    f"[BAIT-{attempt}] ✓✓✓ ENLACE .TXT: {txt_link[:80]}"
                )
                return True

            self._log(
                f"[BAIT-{attempt}] Descarga no detectada aún, "
                f"reintentando secuencia..."
            )

        self._log(f"[BAIT] Secuencia cebo agotada tras 3 intentos")
        return False
    
    def _go_back_if_stuck(self, page: Any) -> bool:
        """Vuelve atrás si se detecta página bloqueada (adultos/spam)."""
        try:
            current = getattr(page, "url", "") or ""
            blocked = ('cam4', 'adult', 'sex', 'porn', 'dating')
            if any(b in current.lower() for b in blocked):
                self._log(f"[BACK] Pagina bloqueada, volviendo atras...")
                try:
                    page.go_back(wait_until='domcontentloaded', timeout=5000)
                    time.sleep(1)
                    new_url = getattr(page, "url", "") or ""
                    self._log(f"[BACK] Ahora en: {new_url[:60]}")
                    return True
                except Exception as e:
                    self._log(f"[BACK] Error: {str(e)[:40]}")
        except Exception:
            pass
        return False

    def _find_txt_link(self, page: Any) -> str | None:
        """Busca un enlace directo a un .txt o a un host de archivos."""
        script = """() => {
            const anchors = Array.from(document.querySelectorAll('a[href]'));
            for (const a of anchors) {
                const href = a.href || '';
                const text = (a.textContent || '').toLowerCase();
                if (/\\.txt(\\?|$)/i.test(href) ||
                    /mediafire|pixeldrain|mega\\.nz|drive\\.google/i.test(href) ||
                    /descargar|download|enlace directo/i.test(text)) {
                    return href;
                }
            }
            return '';
        }"""
        try:
            value = page.evaluate(script)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                self._log(f"Enlace de descarga encontrado: {value[:120]}")
                return value
        except Exception:
            pass
        return None

    @staticmethod
    def _is_file_url(url: str) -> bool:
        low = (url or "").lower()
        return bool(
            re.search(r"\.(txt|zip|rar|7z|pdf|apk|exe|mp4|mkv|mp3)(\?|$)", low)
            or "mediafire.com/file/" in low
            or "pixeldrain.com" in low
        )

    def _run_maze(
        self,
        page: Any,
        context: Any,
        download_holder: dict[str, Any],
        outcome: "BrowserOutcome",
        max_iterations: int = 50,
    ) -> None:
        """
        BOT DE EVASIÓN AUTÓNOMO STEALTH: 50 iteraciones para laberintos de hasta 20+ webs.
        
        Máquina de estados:
        1. Cerrar popups
        2. Eliminar overlays agresivamente
        3. Resolver captchas (30s pausa manual)
        4. Interceptar descargas en segundo plano
        5. Hacer clic en botones visibles
        6. Volver atrás si se detecta botón cebo
        """
        
        deadline = time.monotonic() + min(self.max_sequence_seconds, 2400.0)  # 40 minutos
        captured_download_urls: set[str] = set()
        urls_visited: list[str] = []
        
        # Interceptar respuestas de descarga en segundo plano
        def on_response(response):
            """Captura solo URLs .txt o de dominios de archivos reales."""
            try:
                url = response.url
                low = url.lower()
                archivo = (
                    bool(re.search(r"\.txt(\?|$)", low))
                    or any(h in low for h in (
                        "mediafire.com", "pixeldrain.com", "mega.nz",
                        "drive.google.com", "dropbox.com",
                    ))
                )
                if archivo and url not in captured_download_urls:
                    captured_download_urls.add(url)
                    outcome.extracted_url = url
                    self._log(f"[INTERCEPT] ✓✓✓ URL .TXT: {url[:120]}")
            except Exception:
                pass
        
        # Registrar interceptor
        page.on("response", on_response)
        
# --- MODO PERSISTENCIA EXTREMA -----------------------------------------
        # Atraviesa la cadena completa de blogs de anuncios (hasta 50 saltos).
        # Cada blog tiene un temporizador y un botón de avance que suele ser
        # javascript:void(0) (por eso debe pulsarse por JS, no por href).
        blog_count = 0
        iteration = 0
        last_url_seen = ""
        urls_visitadas: list[str] = []

        self._log("[PERSISTENCIA] Modo Persistencia Extrema activado "
                  "(máx 50 saltos)")

        while iteration < max_iterations:
            iteration += 1
            elapsed = time.monotonic() - (deadline - min(
                self.max_sequence_seconds, 2400.0))
            remaining = min(self.max_sequence_seconds, 2400.0) - elapsed

            if remaining <= 0:
                outcome.timed_out = True
                self._log(f"[PERSISTENCIA-{iteration}] Timeout global "
                          f"({elapsed:.1f}s)")
                break

            # Condición de final: descarga capturada
            if download_holder.get("download") is not None:
                self._log(f"[PERSISTENCIA-{iteration}] ✓ Descarga "
                          f"capturada por Playwright")
                break
            if outcome.extracted_url:
                self._log(f"[PERSISTENCIA-{iteration}] ✓ URL .txt "
                          f"capturada por interceptor")
                break

            current_url = getattr(page, "url", "") or ""

            # Condición de final: ¿hay un enlace .txt directo en el DOM?
            try:
                txt_directo = page.evaluate("""() => {
                    const a = document.querySelector('a[href$=".txt"], '
                        + 'a[href*=".txt?"], a[download]');
                    return a ? (a.href || '') : '';
                }""")
                if txt_directo and "http" in txt_directo:
                    outcome.extracted_url = txt_directo
                    self._log(f"[PERSISTENCIA-{iteration}] ✓✓✓ .TXT EN DOM: "
                              f"{txt_directo[:100]}")
                    break
            except Exception:
                pass

            # Dominios de archivos正品 (donde están los .txt de credenciales).
            # NO incluir dominios genéricos: los blogs tienen enlaces a
            # PDFs de universidades que serían falsos positivos.
            FILE_DOMAINS = (
                "mediafire.com", "drive.google.com", "mega.nz",
                "pixeldrain.com", "dropbox.com", "wetransfer.com",
                "gofile.io", "1fichier.com",
            )
            current_low = current_url.lower()
            es_archivo = (
                bool(re.search(r"\.txt(\?|$)", current_low))
                or self._is_file_url(current_url)
                or any(host in current_low for host in FILE_DOMAINS)
            )
            if es_archivo and not any(
                host in current_low for host in ("uscis.gov", "gov/", ".edu")
            ):
                outcome.extracted_url = current_url
                self._log(f"[PERSISTENCIA-{iteration}] ✓✓✓ DOMINIO DE "
                          f"ARCHIVO: {current_url[:90]}")
                break

            # Detectar salto de blog (cambio de URL)
            if current_url != last_url_seen and last_url_seen:
                blog_count += 1
                self._log(f"Blog de anuncios {blog_count}... saltando al "
                          f"{blog_count + 1}...")
                urls_visitadas.append(current_url)
            last_url_seen = current_url

            self._log(f"[PERSISTENCIA-{iteration}] Blog {blog_count}: "
                      f"{current_url[:80]}")

            # 1) Acelerar temporizadores
            self._inject_timer_accelerator(page)

            # 2) Cerrar popups y overlays
            self._close_popups(context, page)
            try:
                page.evaluate("""() => {
                    document.querySelectorAll('*').forEach(el => {
                        try {
                            const s = window.getComputedStyle(el);
                            const z = parseInt(s.zIndex) || 0;
                            if ((z > 50 || s.position === 'fixed') &&
                                el.offsetHeight > 100) el.remove();
                        } catch(e) {}
                    });
                }""")
            except Exception:
                pass

            # 3) Resolver captcha si aparece
            try:
                if page.locator("div.captcha-box, #recaptcha-anchor").count() > 0:
                    self._solve_challenge(page, time.monotonic() + 120)
            except Exception:
                pass

            # 4) Buscar botón de avance (registrar TODOS los candidatos)
            avance = self._find_advance_button(page)
            try:
                candidatos_info = page.evaluate("""() => {
                    const out = [];
                    document.querySelectorAll('a,button,[role=button]').forEach(e => {
                        const r = e.getBoundingClientRect();
                        if (r.width < 5 || r.height < 5) return;
                        const t = (e.innerText||'').trim().slice(0,40);
                        if (t) out.push(t.replace(/\\n/g,' '));
                    });
                    return [...new Set(out)].slice(0,15);
                }""")
                if candidatos_info:
                    self._log(f"[PERSISTENCIA-{iteration}] Botones vistos: "
                              f"{candidatos_info}")
            except Exception:
                pass

            if avance is None:
                # Esperar a que aparezca el botón tras el temporizador
                self._log(f"[PERSISTENCIA-{iteration}] Esperando botón "
                          f"de avance (timer)...")
                try:
                    page.wait_for_timeout(2500)
                except Exception:
                    time.sleep(2.5)
                avance = self._find_advance_button(page)

            if avance is None:
                self._log(f"[PERSISTENCIA-{iteration}] Sin botón de avance; "
                          f"esperando redirect automático...")
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                time.sleep(2)
                # Si la URL cambió, seguimos; si no, dar un salto atrás
                if getattr(page, "url", "") == current_url:
                    try:
                        page.go_back(wait_until="load", timeout=6000)
                    except Exception:
                        pass
                continue

            # 5) Pulsar el botón de avance
            try:
                texto_avance = (avance.inner_text(timeout=500) or "").strip()[:50]
            except Exception:
                texto_avance = "?"
            self._log(f"[PERSISTENCIA-{iteration}] Pulsando avance: "
                      f"'{texto_avance}'")

            pages_antes = 0
            try:
                pages_antes = len(list(context.pages))
            except Exception:
                pass
            url_antes = current_url

            try:
                avance.click(timeout=4000, force=True)
            except Exception:
                # Los botones javascript:void(0) requieren click por JS
                try:
                    avance.evaluate("el => el.click()")
                except Exception:
                    try:
                        page.evaluate("arguments[0].click()", avance)
                    except Exception:
                        pass

            time.sleep(2)

            # 6) Si se abrió una pestaña nueva, cerrarla y seguir en la principal
            try:
                pages_despues = len(list(context.pages))
            except Exception:
                pages_despues = pages_antes
            if pages_despues > pages_antes:
                self._log(f"[PERSISTENCIA-{iteration}] Pestaña nueva "
                          f"detectada, cerrando...")
                self._close_popups(context, page)
                try:
                    page.bring_to_front()
                except Exception:
                    pass

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # 7) Verificar si hubo progreso
            nueva_url = getattr(page, "url", "") or ""
            if nueva_url != url_antes:
                self._log(f"[PERSISTENCIA-{iteration}] ✓ Avanzó a: "
                          f"{nueva_url[:80]}")
            else:
                self._log(f"[PERSISTENCIA-{iteration}] Sin cambio de URL, "
                          f"continuando...")

        # Limpiar interceptor
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

        if time.monotonic() >= deadline:
            outcome.timed_out = True
        self._log(f"[PERSISTENCIA] Fin tras {blog_count} blogs "
                  f"({iteration}/{max_iterations} iteraciones)")

# Keywords para avanzar por la cadena de blogs de anuncios
    # El botón real de descarga va PRIMERO para no confundirlo con los
    # enlaces publicitarios ("TOKEN NETFLIX VIP", "NEXT POST", etc.).
    ADVANCE_KEYWORDS = (
        "download — descarga aquí", "descarga aquí", "download",
        "descargar", "descarga directa", "obtener enlace", "get link",
        "ir al enlace", "ir a la descarga", "continuar al enlace",
        "continuar", "siguiente", "next post", "next", "paso", "step",
        "skip", "generar enlace", "ver enlace", "continue",
    )

    def _find_advance_button(self, page: Any) -> Any | None:
        """Busca heurísticamente el botón/enlace para avanzar al siguiente blog.

        Prioridad:
        1. IDs conocidos de avance (#nextPostLink, #wpsafelinkhuman, ...).
        2. Enlaces/botones visibles cuyo texto contenga un keyword de avance.
        Se ignoran elementos de accesibilidad (screen-reader-text) y los que
        no están realmente visibles.
        """
        # 1) IDs conocidos de avance en blogs de anuncios
        for button_id in (
            "nextPostLink", "wpsafelinkhuman", "wpsafegenerate",
            "wpsafe-generate", "wpsafe-link", "waiting-btn", "mainButton",
            "get-link", "getlink", "download", "downloadButton",
        ):
            try:
                loc = page.locator(f"#{button_id}")
                # Los dobles de test pueden no implementar count()
                if hasattr(loc, "count"):
                    if loc.count() > 0 and loc.first.is_visible(timeout=600):
                        return loc.first
                elif loc.is_visible(timeout=600):
                    return loc
            except Exception:
                pass

        # 2) Búsqueda por texto con RANKING de prioridad.
        #    Se recorren todas las coincidencias y se elige la de mayor
        #    prioridad (botón de descarga > cebo > NEXT POST).
        candidatos = page.locator(
            "a, button, input[type=submit], input[type=button], [role=button]"
        )
        try:
            total = candidatos.count()
        except Exception:
            total = 0
        mejor = None
        mejor_prioridad = 999
        for index in range(min(total, 200)):
            item = candidatos.nth(index)
            try:
                if not item.is_visible(timeout=200):
                    continue
                try:
                    cls = (item.get_attribute("class") or "").lower()
                    if "screen-reader-text" in cls or "skip-link" in cls:
                        continue
                except Exception:
                    pass
                texto = ""
                try:
                    texto = (item.inner_text() or item.get_attribute("value")
                             or item.get_attribute("aria-label") or "")
                except Exception:
                    pass
                low = re.sub(r"\s+", " ", texto.strip().lower())
                if not low:
                    continue
                # prioridad = índice del keyword que coincide (0 = máxima)
                for prioridad, keyword in enumerate(self.ADVANCE_KEYWORDS):
                    if keyword in low and prioridad < mejor_prioridad:
                        mejor_prioridad = prioridad
                        mejor = item
                        break
            except Exception:
                continue
        if mejor is not None:
            return mejor

        # 2b) El botón cerrado por CSS (display:none / opacity:0) también
        #     cuenta: en estos blogs el botón de descarga aparece oculto
        #     hasta que se pulsa el cebo. Lo devolvemos como último recurso.
        for keyword in self.ADVANCE_KEYWORDS[:6]:
            try:
                oculto = page.locator(
                    f"text={keyword}"
                ).first
                if oculto.count() > 0:
                    return oculto
            except Exception:
                continue

        # 3) Fallback: get_by_text (compatible con dobles de test)
        for keyword in self.ADVANCE_KEYWORDS:
            try:
                loc = page.get_by_text(keyword, exact=False)
                if hasattr(loc, "count"):
                    if loc.count() == 0:
                        continue
                    loc = loc.first
                if loc.is_visible(timeout=200):
                    return loc
            except Exception:
                continue
        return None

    def _inject_timer_accelerator(self, page: Any) -> None:
        """Acelera setTimeout/setInterval y elimina meta refresh."""
        try:
            page.evaluate("""() => {
                const MAX_DELAY = 50;
                const realSetTimeout = window.setTimeout.bind(window);
                const realSetInterval = window.setInterval.bind(window);
                window.setTimeout = function (fn, delay, ...args) {
                    const ms = typeof delay === "number"
                        ? Math.min(delay, MAX_DELAY) : delay;
                    return realSetTimeout(fn, ms, ...args);
                };
                window.setInterval = function (fn, delay, ...args) {
                    const ms = typeof delay === "number"
                        ? Math.min(delay, MAX_DELAY) : delay;
                    return realSetInterval(fn, ms, ...args);
                };
                document.querySelectorAll(
                    'meta[http-equiv="refresh"]'
                ).forEach(m => m.remove());
            }""")
        except Exception:
            pass

    @staticmethod
    def _read_captcha_buttons(page: Any) -> tuple[str, list[str]]:
        """Lee el enunciado y los data-URL de las imágenes de cada botón."""
        try:
            data = page.evaluate(
                """() => {
                    const box = document.querySelector('div.captcha-box');
                    let prompt = box ? (box.innerText || '').trim() : '';
                    if (!prompt) {
                        prompt = (document.body.innerText || '')
                            .split('\\n')
                            .find(line => /n[uú]mero|number/i.test(line)) || '';
                    }
                    const sources = [];
                    const scope = box || document;
                    scope.querySelectorAll('button img').forEach(img => {
                        if (img.src) sources.push(img.src);
                    });
                    if (!sources.length) {
                        document.querySelectorAll('button img[alt="captcha"]')
                            .forEach(img => {
                                if (img.src) sources.push(img.src);
                            });
                    }
                    return {prompt: prompt, sources: sources};
                }"""
            )
        except Exception:
            return "", []
        if not isinstance(data, dict):
            return "", []
        prompt = str(data.get("prompt") or "")
        sources = [str(src) for src in (data.get("sources") or []) if src]
        return prompt, sources

    def _solve_number_captcha_ocr(self, page: Any) -> bool | None:
        """Resuelve el captcha numérico con OCR.

        Prueba los botones candidatos en orden de probabilidad (el servidor
        permite pocos intentos por desafío). Devuelve ``True`` si acertó,
        ``False`` si falló y ``None`` si el OCR no está disponible.
        """
        try:
            import captcha_ocr
        except Exception as exc:
            self._log(f"OCR de captcha no disponible: {exc}")
            return None
        if not captcha_ocr.available():
            self._log("OCR de captcha no disponible (falta easyocr/opencv)")
            return None

        prompt, sources = self._read_captcha_buttons(page)
        if not sources:
            return False

        target = captcha_ocr.parse_target_number(prompt)
        self._log(f"captcha OCR: '{prompt[:60]}' ({len(sources)} botones)")

        ranked, readings = captcha_ocr.solve_buttons(prompt, sources)
        preview = ", ".join(
            f"{i}:[{', '.join(c[0] for c in cands[:2])}]"
            for i, cands in enumerate(readings)
        )
        self._log(f"captcha OCR lecturas: {preview}")
        self._log(f"captcha OCR candidatos ordenados: {ranked} (objetivo {target})")

        if not ranked:
            self._log(
                f"captcha OCR: ningún botón parece el {target}"
            )
            return False

        start_url = getattr(page, "url", "") or ""

        for index in ranked[:3]:
            self._log(f"captcha OCR: probando botón {index}")
            try:
                button = page.locator("div.captcha-box button").nth(index)
                if button.count() == 0:
                    button = page.locator("button").nth(index)
                button.click(timeout=4000, force=True)
            except Exception:
                try:
                    page.locator("button").nth(index).evaluate(
                        "el => el.click()"
                    )
                except Exception as exc:
                    self._log(f"captcha OCR: clic falló: {exc}")
                    continue

            # Verificar resultado.
            outcome = "unknown"
            for _ in range(40):
                try:
                    page.wait_for_timeout(250)
                except Exception:
                    time.sleep(0.25)
                try:
                    texto = page.inner_text("body")
                except Exception:
                    texto = ""
                if re.search(r"Verificado|Verified|success", texto, re.I):
                    self._log("captcha OCR: verificado")
                    return True
                if (getattr(page, "url", "") or "") != start_url:
                    self._log("captcha OCR: navegación tras verificar")
                    return True
                if re.search(r"Int[eé]ntalo de nuevo|Try again", texto, re.I):
                    outcome = "rejected"
                    break
                if re.search(r"Demasiados intentos", texto, re.I):
                    self._log("captcha OCR: servidor bloqueó los intentos")
                    return False
                if re.search(r"Selecciona el n|select_number|number \d",
                             texto, re.I) and _ > 14:
                    outcome = "rejected"
                    break
            if outcome == "rejected":
                # El sitio regenera el desafío: hay que releer las imágenes.
                self._log("captcha OCR: desafío rechazado")
                try:
                    page.wait_for_selector("div.captcha-box button",
                                           timeout=15000)
                except Exception:
                    return False
                prompt, sources = self._read_captcha_buttons(page)
                if not sources:
                    return False
                ranked, readings = captcha_ocr.solve_buttons(prompt, sources)
                if not ranked:
                    return False
                continue
        return False

    def _captcha_human_assist(self, page: Any, deadline: float) -> bool:
        """Pide a la persona que resuelva el captcha en el navegador visible.

        Espera a que el desafío desaparezca (resuelto) o a que se agote el
        tiempo. Devuelve True si se resolvió.
        """
        try:
            prompt, _sources = self._read_captcha_buttons(page)
        except Exception:
            prompt = ""
        mensaje = (
            "\n"
            + "=" * 64
            + "\n  CAPTCHA: el OCR no acertó. Resuélvelo en la ventana"
              " del navegador.\n"
        )
        if prompt:
            mensaje += f"  Pista: {prompt.strip()}\n"
        mensaje += "  El bot continuará solo en cuanto lo aciertes.\n" + "=" * 64
        try:
            print(mensaje, file=sys.stderr, flush=True)
        except Exception:
            pass
        start_url = getattr(page, "url", "") or ""
        while time.monotonic() < deadline:
            try:
                page.wait_for_timeout(700)
            except Exception:
                time.sleep(0.7)
            try:
                texto = page.inner_text("body")
            except Exception:
                texto = ""
            if not re.search(
                r"Selecciona el n|select_number|number \d|Cargando el desaf",
                texto,
                re.I,
            ):
                self._log("captcha: resuelto manualmente")
                return True
            if (getattr(page, "url", "") or "") != start_url:
                self._log("captcha: navegación tras resolución manual")
                return True
        self._log("captcha: se agotó el tiempo esperando resolución manual")
        return False

    def _solve_challenge(self, page: Any, deadline: float) -> bool:
        """Resuelve el captcha de ICutLink ("Selecciona el número N").

        Primero intenta leer los números de las imágenes con OCR y pulsar el
        correcto. Si el OCR no está disponible, cae al método anterior de
        pulsaciones aleatorias (cada fallo recarga un desafío nuevo).
        """
        self._log("posible captcha: esperando desafío…")
        patron = re.compile(r"select_number|Selecciona el n[uú]mero|Verify You")
        texto = ""
        limite = time.monotonic() + 8.0
        while time.monotonic() < limite:
            try:
                texto = page.inner_text("body")
            except Exception:
                texto = ""
            if patron.search(texto):
                break
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
        if not patron.search(texto):
            return False

        ocr_ready: bool | None = None
        ocr_failures = 0
        for attempt in range(60):
            if time.monotonic() >= deadline:
                return False
            # Esperar a que el desafío (re)aparezca tras un fallo.
            try:
                page.wait_for_function(
                    "() => /Selecciona el n|select_number|number \\d/i"
                    ".test(document.body.innerText)",
                    timeout=12000,
                )
            except Exception:
                continue

            # Tras varios fallos del OCR, pedir ayuda a la persona.
            if ocr_failures >= 3 and not self.headless:
                if self._captcha_human_assist(page, deadline):
                    return True
                return False

            # 1) Intento con OCR.
            if ocr_ready is not False:
                result = self._solve_number_captcha_ocr(page)
                if result is None:
                    ocr_ready = False
                    self._log("captcha: OCR no disponible, uso pulsaciones")
                else:
                    ocr_ready = True
                    if result:
                        self._log("captcha de ICutLink resuelto (OCR)")
                        try:
                            page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass
                        try:
                            page.wait_for_timeout(2000)
                        except Exception:
                            time.sleep(2.0)
                        return True
                    ocr_failures += 1
                    # Desafío nuevo tras fallar: dar un respiro.
                    try:
                        page.wait_for_timeout(800)
                    except Exception:
                        time.sleep(0.8)
                    continue

            # 2) Fallback: pulsación aleatoria.
            start_url = getattr(page, "url", "") or ""
            try:
                total = page.locator("button").count()
            except Exception:
                total = 0
            if total <= 0:
                try:
                    page.wait_for_timeout(700)
                except Exception:
                    time.sleep(0.7)
                continue
            idx = random.randrange(total)
            self._log(f"captcha: probando botón {idx + 1}/{total}")
            try:
                page.locator("button").nth(idx).evaluate("el => el.click()")
            except Exception as exc:
                self._log(f"captcha: clic falló: {exc}")
            try:
                page.wait_for_timeout(1000)
            except Exception:
                time.sleep(1.0)
            if (getattr(page, "url", "") or "") == start_url:
                try:
                    page.wait_for_function(
                        "() => !/Selecciona el n|select_number/i"
                        ".test(document.body.innerText)",
                        timeout=8000,
                    )
                except Exception:
                    pass
            try:
                texto = page.inner_text("body")
            except Exception:
                texto = ""
            avanzado = (getattr(page, "url", "") or "") != start_url
            if re.search(r"Verificado|Verified|success", texto, re.I) or avanzado:
                self._log("captcha de ICutLink resuelto")
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                try:
                    page.wait_for_timeout(2000)
                except Exception:
                    time.sleep(2.0)
                return True
        return False

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
                url_antes = getattr(page, "url", "") or ""
                if self._click(page, action, deadline):
                    etiqueta = f"{action.selector or ''} {action.text or ''}".lower()
                    if any(
                        clave in etiqueta
                        for clave in ("mainbutton", "download", "descargar")
                    ):
                        # Este clic dispara una petición y luego navega: esperar
                        # a que la URL cambie para no extraer la página vieja.
                        try:
                            page.wait_for_function(
                                "(u) => location.href !== u", url_antes, timeout=15000
                            )
                        except Exception:
                            pass
                    self._wait(page)
                    if not action.keep_popup:
                        self._close_extra_tabs(extra_pages, page)
                    self._log(f"url tras clic: {getattr(page, 'url', '')}")
            elif kind == "SHORTEN":
                current = getattr(page, "url", "") or ""
                if "vercel.app" in current and "/api/shorten" not in current:
                    self._log("wrapper detectado; llamando a su API")
                    try:
                        target = page.evaluate(
                            """async () => {
                                const u = new URL(location.href).searchParams.get('url');
                                if (!u) return '';
                                const r = await fetch('/api/shorten?url=' + encodeURIComponent(u));
                                const d = await r.json();
                                return d.shortenedUrl || '';
                            }"""
                        )
                    except Exception as exc:
                        target = ""
                        self._log(f"API del wrapper falló: {exc}")
                    if target:
                        try:
                            page.goto(target, wait_until="domcontentloaded", timeout=30000)
                        except Exception as exc:
                            self._log(f"navegación al acortador falló: {exc}")
                        # El wrapper ya nos dio la URL acortada: cualquier paso
                        # posterior del plan (pensado para la página del
                        # wrapper) solo pulsaría anuncios. Devolvemos el
                        # control al resolver para que siga desde aquí.
                        outcome.extracted_url = target
                        self._log(
                            f"wrapper resuelto; continuar desde {target[:80]}"
                        )
                        break
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
                launch_kwargs: dict[str, Any] = {"headless": self.headless}
                if self.slow_mo:
                    launch_kwargs["slow_mo"] = int(self.slow_mo)
                browser = playwright.chromium.launch(**launch_kwargs)
                context = browser.new_context(
                    user_agent=self.user_agent,
                    viewport={"width": 1366, "height": 768},
                    accept_downloads=True,
                    extra_http_headers=HEADLESS_MASK_HEADERS,
                )
                
                # Aplicar stealth mode para evasión de detección de bots
                # (debe hacerse sobre la página, así que se aplica tras crearla)
                if speed_up_timers or self.maze:
                    try:
                        context.add_init_script(TIMER_EVASION_SCRIPT)
                    except Exception:
                        pass
                initial_cookies = self._cookies_for_context(self.cookies)
                if initial_cookies:
                    try:
                        context.add_cookies(initial_cookies)
                    except Exception:
                        pass

                page = context.new_page()
                # Enmascarar navigator.webdriver para que las webs no
                # oculten contenido ni fuercen captchas extra.
                try:
                    _apply_stealth(page)
                except Exception as exc:
                    self._log(f"stealth: {exc}")
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

                try:
                    self._solve_challenge(
                        page,
                        time.monotonic()
                        + max(self.max_wait, self.max_sequence_seconds),
                    )
                except Exception as exc:
                    self._log(f"captcha no resuelto: {exc}")

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

                if (
                    self.maze
                    and download_holder.get("download") is None
                    and not outcome.extracted_url
                ):
                    try:
                        self._run_maze(page, context, download_holder, outcome)
                    except Exception as exc:
                        self._log(f"laberinto interrumpido: {exc}")

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
    maze: bool = True,
    debug: bool = False,
    slow_mo: int | None = None,
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
        maze=maze,
        debug=debug,
        slow_mo=slow_mo,
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
