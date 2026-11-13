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
    from playwright_stealth import stealth_sync
except ImportError:
    def stealth_sync(context):
        """Fallback si playwright-stealth no está instalado"""
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
        """Cierra pestañas basura (adult/spam) y devuelve el foco a `page`."""
        closed = 0
        blocked = ('cam4', 'adult', 'sex', 'porn', 'dating', 'xnxx', 'xvideos')
        try:
            pages = list(context.pages)
        except Exception:
            return 0
        for candidate in pages:
            if candidate is page:
                continue
            try:
                url = candidate.url.lower()
                if any(b in url for b in blocked):
                    self._log(f"[BLOCK] Cerrando popup bloqueado: {url[:60]}")
                    candidate.close()
                    closed += 1
                    continue
                candidate.close()
                closed += 1
            except Exception:
                pass
        if closed:
            self._log(f"[POPUPS] Cerradas {closed} pestana(s)")
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
                    if loc.is_visible(timeout=200):
                        self._log(f"Botón por texto (get_by_text): {texto!r}")
                        return loc
                except Exception:
                    continue
        return None

    def _remove_blocking_overlays(self, page: Any) -> None:
        """Elimina AGRESIVAMENTE overlays, iframes y publicidad que bloquean clics."""
        try:
            removed = page.evaluate("""() => {
                let count = 0;
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
                try {
                    document.querySelectorAll('iframe').forEach(el => {
                        const s = (el.src || '').toLowerCase();
                        const i = (el.id || '').toLowerCase();
                        if (s.includes('ad') || s.includes('banner') || i.includes('ad') || i.includes('popup')) {
                            try { el.remove(); count++; } catch(e) {}
                        }
                    });
                } catch(e) {}
                try {
                    document.querySelectorAll('[class*="ad"], [class*="popup"], [class*="modal"]').forEach(el => {
                        if (el.offsetHeight > 50) try { el.remove(); count++; } catch(e) {}
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
        """Busca botón cebo: Continuar, Siguiente, etc (NO Descarga Directa)."""
        keywords = ['continuar', 'continue', 'siguiente', 'next', 'pulsa', 'click', 'acceso', 'ver']
        try:
            items = page.locator('a, button, input[type=submit], [role=button]')
            for idx in range(min(items.count(), 100)):
                try:
                    item = items.nth(idx)
                    if not item.is_visible(timeout=200):
                        continue
                    text = ""
                    try:
                        text = item.inner_text() or ""
                    except:
                        pass
                    if not text:
                        try:
                            text = item.get_attribute("value") or ""
                        except:
                            pass
                    low = text.lower().strip()
                    if any(kw in low for kw in keywords):
                        if 'descarga' not in low or 'directo' not in low:
                            return item
                except:
                    pass
        except:
            pass
        return None
    
    def _find_real_download_button(self, page: Any) -> Any | None:
        """Busca botón real: Descarga Directa, .txt, Download, etc."""
        keywords = ['descarga directa', 'direct download', 'download', 'descargar archivo', 'enlace directo']
        try:
            # Primero buscar enlaces .txt
            try:
                links = page.locator('a[href*=".txt"], a[download]')
                for i in range(min(links.count(), 10)):
                    try:
                        if links.nth(i).is_visible(timeout=200):
                            return links.nth(i)
                    except:
                        pass
            except:
                pass
            
            # Buscar por keyword
            items = page.locator('a, button, input[type=submit], [role=button]')
            for idx in range(min(items.count(), 100)):
                try:
                    item = items.nth(idx)
                    if not item.is_visible(timeout=200):
                        continue
                    text = ""
                    try:
                        text = item.inner_text() or ""
                    except:
                        pass
                    if any(kw in text.lower() for kw in keywords):
                        return item
                except:
                    pass
        except:
            pass
        return None
    
    def _bait_and_back_sequence(self, page: Any, context: Any) -> bool:
        """Secuencia: cebo → popup → atrás → descarga real."""
        for attempt in range(1, 4):
            self._log(f"[BAIT-{attempt}] Buscando botón cebo...")
            bait = self._find_bait_button(page)
            if not bait:
                return False
            
            try:
                text = bait.inner_text(timeout=500).strip()[:50]
            except:
                text = "?"
            
            self._log(f"[BAIT-{attempt}] Pulsando: {text}")
            try:
                bait.click(timeout=3000, force=True)
            except:
                try:
                    bait.evaluate("el => el.click()")
                except:
                    return False
            
            self._log(f"[BAIT-{attempt}] Cerrando popup...")
            time.sleep(2)
            self._close_popups(context, page)
            
            self._log(f"[BAIT-{attempt}] Volviendo atrás...")
            try:
                page.go_back(wait_until='domcontentloaded', timeout=10000)
            except Exception as e:
                self._log(f"[BAIT-{attempt}] Error: {str(e)[:40]}")
                return False
            
            time.sleep(2)
            try:
                page.wait_for_load_state('networkidle', timeout=15000)
            except:
                pass
            
            self._log(f"[BAIT-{attempt}] Buscando botón real...")
            real = self._find_real_download_button(page)
            if not real:
                self._log(f"[BAIT-{attempt}] No encontrado, reintentando...")
                continue
            
            try:
                rtext = real.inner_text(timeout=500).strip()[:50]
            except:
                rtext = "?"
            
            self._log(f"[BAIT-{attempt}] ✓ Encontrado: {rtext}")
            
            try:
                real.click(timeout=3000, force=True)
            except:
                try:
                    real.evaluate("el => el.click()")
                except:
                    continue
            
            self._wait(page)
            url = getattr(page, "url", "") or ""
            if self._is_file_url(url):
                self._log(f"[BAIT-{attempt}] ✓✓✓ DESCARGA: {url[:80]}")
                return True
            
            time.sleep(1)
        
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
            """Captura automáticamente URLs de .txt descargadas"""
            try:
                url = response.url
                content_type = response.headers.get("content-type", "").lower()
                
                # Detectar descarga de .txt o archivo binario
                if (".txt" in url.lower() or 
                    "text/plain" in content_type or
                    "application/octet-stream" in content_type or
                    "attachment" in content_type):
                    
                    if url not in captured_download_urls:
                        captured_download_urls.add(url)
                        self._log(f"[INTERCEPT] Descarga capturada en background: {url[:100]}")
                        
                        # Si es .txt, guardar inmediatamente
                        if ".txt" in url.lower():
                            outcome.extracted_url = url
                            self._log(f"[INTERCEPT] ✓✓✓ URL .TXT CAPTURADA: {url}")
            except:
                pass
        
        # Registrar interceptor
        page.on("response", on_response)
        
        # Máquina de estados de 50 iteraciones
        iteration = 0
        stuck_counter = 0
        
        while iteration < max_iterations:
            iteration += 1
            elapsed = time.monotonic() - (deadline - min(self.max_sequence_seconds, 2400.0))
            remaining = min(self.max_sequence_seconds, 2400.0) - elapsed
            
            if remaining <= 0:
                outcome.timed_out = True
                self._log(f"[STEALTH-{iteration}] Timeout global ({elapsed:.1f}s)")
                break
            
            if download_holder.get("download") is not None:
                self._log(f"[STEALTH-{iteration}] ✓ Descarga capturada por Playwright")
                break
            
            if outcome.extracted_url:
                self._log(f"[STEALTH-{iteration}] ✓ URL .txt capturada por interceptor")
                break
            
            current_url = getattr(page, "url", "") or ""
            self._log(f"[STEALTH-{iteration}] Web: {current_url[:80]}")
            
            # ESTADO 1: CERRAR POPUPS
            try:
                self._close_popups(context, page)
            except:
                pass
            
            # ESTADO 2: ELIMINAR OVERLAYS AGRESIVAMENTE
            try:
                removed = page.evaluate("""() => {
                    let count = 0;
                    document.querySelectorAll('*').forEach(el => {
                        try {
                            const z = parseInt(window.getComputedStyle(el).zIndex) || 0;
                            const pos = window.getComputedStyle(el).position;
                            if ((z > 50 || pos === 'fixed') && el.offsetHeight > 100) {
                                el.remove();
                                count++;
                            }
                        } catch(e) {}
                    });
                    return count;
                }""")
                if removed > 0:
                    self._log(f"[STEALTH-{iteration}] Eliminados {removed} overlays")
            except:
                pass
            
            # ESTADO 3: DETECTAR Y RESOLVER CAPTCHA
            try:
                has_captcha = page.evaluate("""() => {
                    return !!document.querySelector('iframe[src*="recaptcha"], [class*="turnstile"], input[data-sitekey]');
                }""")
                
                if has_captcha:
                    self._log(f"[STEALTH-{iteration}] ¡ATENCIÓN! Captcha detectado. Resuelve a mano en 30s...")
                    try:
                        page.wait_for_timeout(30000)
                    except:
                        time.sleep(30)
                    self._log(f"[STEALTH-{iteration}] Continuando después de pausa de captcha")
            except:
                pass
            
            # ESTADO 4: ESPERAR CARGA
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except:
                pass
            
            # ESTADO 5: BUSCAR BOTÓN VISIBLE
            boton = None
            try:
                boton = self._find_text_button(page)
            except:
                pass
            
            if boton is None:
                self._log(f"[STEALTH-{iteration}] Sin botón. Esperando 2s...")
                time.sleep(2)
                try:
                    boton = self._find_text_button(page)
                except:
                    pass
            
            # Si no hay botón, intentar avanzar en historial
            if boton is None:
                self._log(f"[STEALTH-{iteration}] Sin acciones. go_forward()...")
                try:
                    page.go_forward(wait_until='load', timeout=5000)
                    stuck_counter = 0
                    time.sleep(2)
                    continue
                except:
                    stuck_counter += 1
                    if stuck_counter > 3:
                        self._log(f"[STEALTH-{iteration}] Atascado. go_back()...")
                        try:
                            page.go_back(wait_until='load', timeout=5000)
                            stuck_counter = 0
                        except:
                            pass
                    continue
            
            # ESTADO 6: HACER CLIC EN BOTÓN
            url_antes = current_url
            
            try:
                texto = boton.inner_text(timeout=500).strip()[:60]
            except:
                texto = "?"
            
            self._log(f"[STEALTH-{iteration}] Pulsando: {texto}")
            
            try:
                boton.click(timeout=3000, force=True)
            except:
                try:
                    page.evaluate("arguments[0].click()", boton)
                except:
                    self._log(f"[STEALTH-{iteration}] Error al pulsar")
                    continue
            
            # Esperar navegación
            time.sleep(2)
            
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except:
                pass
            
            url_despues = getattr(page, "url", "") or ""
            
            # ESTADO 7: DETECTAR BOTÓN CEBO (SIN PROGRESO)
            if url_despues == url_antes:
                stuck_counter += 1
                self._log(f"[STEALTH-{iteration}] Botón cebo detectado (x{stuck_counter})")
                
                # Ir hacia atrás después de 2 intentos
                if stuck_counter > 2:
                    self._log(f"[STEALTH-{iteration}] go_back() después de botón cebo")
                    try:
                        page.go_back(wait_until='load', timeout=8000)
                        time.sleep(2)
                        stuck_counter = 0
                    except:
                        pass
            else:
                stuck_counter = 0
                self._log(f"[STEALTH-{iteration}] ✓ Progreso: {url_despues[:60]}")
                urls_visited.append(url_despues)
        
        # Limpiar interceptor
        try:
            page.remove_listener("response", on_response)
        except:
            pass
        
        if time.monotonic() >= deadline:
            outcome.timed_out = True
            self._log(f"[STEALTH] Timeout - {iteration}/{max_iterations} iteraciones")

    def _solve_challenge(self, page: Any, deadline: float) -> bool:
        """Resuelve el captcha de ICutLink ("Selecciona el número N").

        Los botones son imágenes sin texto, así que se prueban pulsaciones
        aleatorias; cada fallo recarga un desafío nuevo. Se para al ver el
        aviso de verificado o al cambiar de página.
        """
        self._log("posible captcha: esperando desafío…")
        patron = re.compile(r"select_number|Selecciona el n[uú]mero|Verify You")
        texto = ""
        limite = time.monotonic() + 6.0
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
        start_url = getattr(page, "url", "") or ""
        for _ in range(60):
            if time.monotonic() >= deadline:
                return False
            # Esperar a que el desafío (re)aparezca tras un fallo.
            try:
                page.wait_for_function(
                    "() => /Selecciona el n|select_number/i.test(document.body.innerText)",
                    timeout=12000,
                )
            except Exception:
                continue
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
            # Esperar a que el desafío viejo desaparezca antes de intentar otro.
            if (getattr(page, "url", "") or "") == start_url:
                try:
                    page.wait_for_function(
                        "() => !/Selecciona el n|select_number/i.test(document.body.innerText)",
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
                # Dejar que la pasarela termine su POST/redirección.
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
                try:
                    stealth_sync(context)
                except Exception as e:
                    self._log(f"[STEALTH] No se pudo aplicar stealth: {e}")
                
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
