"""Motor de resolución: sigue la cadena de acortadores hasta el enlace final.

Este módulo sólo resuelve (no descarga). La descarga está en ``downloader.py``
y la coordinación entre CLI y web está en ``orchestrator.py``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlsplit

import requests

from browser_resolver import (
    BrowserOutcome,
    BrowserResolver,
    is_bot_challenge,
)
from browser_resolver import browser_resolve_and_download as _browser_resolve_and_download
from desenlace_core import (
    CACHE_FILE,
    DEFAULT_CACHE_TTL,
    DEFAULT_MAX_HOPS,
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_MAX_WAIT,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DEFAULT_UA,
    DEFAULT_WAIT_BUFFER,
    TRANSIENT_STATUS,
    URL_IN_TEXT_RE,
    ResolveError,
    ensure_extension,
    extract_wrapper_target,
    filename_from_disposition,
    filename_from_url,
    is_plausible_url,
    next_url_from_payload,
    origin_of,
    redact_url,
    sanitize_filename,
)
from downloader import download_resolution
from models import Resolution
from plugins import build_browser_plan, detect_action, looks_like_shortener, normalize_url
from plugins.redirectors import extract_redirect_target
from safe_http import SafeSession

LogFunction = Callable[[str], None]

# Compatibilidad: nombre histórico del detector de challenges.
is_challenge = is_bot_challenge


def _default_log(lock: threading.Lock | None) -> LogFunction:
    def write(line: str) -> None:
        if lock is None:
            print(line, file=sys.stderr, flush=True)
        else:
            with lock:
                print(line, file=sys.stderr, flush=True)

    return write


def cache_key(url: str) -> str:
    """Normaliza la clave de caché: wrapper y destino directo comparten entrada."""
    return (extract_wrapper_target(url) or url).strip()


class ResolutionCache:
    """Caché local y thread-safe de páginas finales estables (sin tokens)."""

    def __init__(
        self,
        path: str | Path = CACHE_FILE,
        *,
        ttl: float = DEFAULT_CACHE_TTL,
        enabled: bool = True,
    ) -> None:
        self.path = Path(path)
        self.ttl = ttl
        self.enabled = enabled
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self.enabled:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, dict)
        }

    def _save(self) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
        except OSError:
            pass

    def get(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        with self._lock:
            entry = self._data.get(key)
            if not isinstance(entry, dict):
                return None
            try:
                saved_at = float(entry.get("saved_at", 0))
            except (TypeError, ValueError):
                saved_at = 0
            if time.time() - saved_at > self.ttl:
                self._data.pop(key, None)
                self._save()
                return None
            return dict(entry)

    def put(self, key: str, page_url: str, filename: str) -> None:
        if self.enabled is False or not page_url:
            return
        with self._lock:
            self._data[key] = {
                "page_url": page_url,
                "filename": filename,
                "saved_at": time.time(),
            }
            self._save()

    def remove(self, key: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._data.pop(key, None) is not None:
                self._save()



def is_html_response(response: requests.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    if "html" in content_type or "xhtml" in content_type:
        return True
    if content_type:
        return False
    return True


def load_cookie_file(path: str | Path) -> requests.cookies.RequestsCookieJar:
    """Carga cookies desde un archivo Netscape o una lista `nombre=valor`.

    Es útil para acortadores protegidos por Cloudflare (`cf_clearance`) o por
    sesión. Nunca se registran los valores en los logs.
    """
    from http.cookiejar import MozillaCookieJar

    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ResolveError(f"No existe el archivo de cookies: {file_path}")
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise ResolveError(f"No se pudo leer {file_path}: {exc}") from exc

    jar = requests.cookies.RequestsCookieJar()
    is_netscape = any(line.count("\t") >= 6 for line in text.splitlines())
    if is_netscape:
        mozilla = MozillaCookieJar(str(file_path))
        try:
            mozilla.load(ignore_discard=True, ignore_expires=True)
        except (OSError, ValueError) as exc:
            raise ResolveError(f"Archivo de cookies inválido: {exc}") from exc
        for cookie in mozilla:
            jar.set_cookie(cookie)
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("cookie:"):
                line = line[7:].strip()
            for pair in line.split(";"):
                if "=" not in pair:
                    continue
                name, value = pair.split("=", 1)
                name = name.strip()
                if name:
                    jar.set(name, value.strip())

    if not len(jar):
        raise ResolveError(f"El archivo de cookies no contiene cookies: {file_path}")
    return jar


class LinkResolver:
    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_wait: float = DEFAULT_MAX_WAIT,
        max_hops: int = DEFAULT_MAX_HOPS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        verbose: bool = False,
        full_chain: bool = False,
        wait_buffer: float = DEFAULT_WAIT_BUFFER,
        cache: ResolutionCache | None = None,
        log_lock: threading.Lock | None = None,
        prefix: str = "",
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
        cookies: str | Path | requests.cookies.RequestsCookieJar | None = None,
        retries: int = DEFAULT_RETRIES,
        allow_private: bool = False,
        browser_fallback: bool = False,
        logger: LogFunction | None = None,
        trace: list[dict[str, Any]] | None = None,
        browser_timeout: float = 120.0,
        max_size: int | None = None,
        debug_browser: bool = False,
    ) -> None:
        self.logger: LogFunction = logger or _default_log(log_lock)
        self.trace: list[dict[str, Any]] = trace if trace is not None else []
        self.browser_timeout = max(5.0, float(browser_timeout))
        self.debug_browser = debug_browser
        self.max_size = max_size
        self.timeout = timeout
        self.max_wait = max_wait
        self.max_hops = max_hops
        self.max_redirects = max(0, int(max_redirects))
        self.verbose = verbose
        self.full_chain = full_chain
        self.wait_buffer = max(0.0, wait_buffer)
        self.cache = cache
        self.log_lock = log_lock
        self.prefix = prefix
        self.retries = max(0, int(retries))
        self.allow_private = allow_private
        self.browser_fallback = browser_fallback
        self._browser_attempted: set[str] = set()
        self.session = SafeSession(
            allow_private=allow_private, max_redirects=self.max_redirects
        )
        self.session.headers.update(
            {
                "User-Agent": user_agent or DEFAULT_UA,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        if headers:
            self.session.headers.update(headers)
        if cookies is not None:
            if isinstance(cookies, (str, Path)):
                jar = load_cookie_file(cookies)
            else:
                jar = cookies
            self.session.cookies.update(jar)
        self.session.max_redirects = max(1, self.max_redirects)

    def _log(self, message: str) -> None:
        self.logger(f"{self.prefix}{message}")

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        referer: str | None = None,
        stream: bool = True,
    ) -> requests.Response:
        request_headers: dict[str, str] = {}
        if headers:
            request_headers.update(headers)
        if method.upper() == "POST" and "Origin" not in request_headers:
            request_headers["Origin"] = origin_of(url)
        if referer and "Referer" not in request_headers:
            request_headers["Referer"] = referer

        attempts = self.retries + 1
        is_get = method.upper() == "GET"
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    url,
                    data=data,
                    headers=request_headers,
                    timeout=self.timeout,
                    allow_redirects=True,
                    stream=stream,
                )
            except requests.TooManyRedirects as exc:
                raise ResolveError(
                    f"Se superó el límite de {self.max_redirects} redirecciones "
                    f"en {redact_url(url)}"
                ) from exc
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                backoff = min(8.0, 0.6 * (2**attempt))
                self._log(f"    error de red ({exc}); retry en {backoff:.1f}s")
                time.sleep(backoff)
                continue

            transient = response.status_code in TRANSIENT_STATUS
            retriable = transient and (is_get or response.status_code == 429)
            if retriable and attempt + 1 < attempts:
                response.close()
                backoff = min(8.0, 0.6 * (2**attempt))
                self._log(
                    f"    HTTP {response.status_code} transitorio; "
                    f"reintento en {backoff:.1f}s"
                )
                time.sleep(backoff)
                continue
            return response

        raise ResolveError(
            f"No se pudo conectar con {redact_url(url)}: {last_error}"
        )

    def _wait(self, seconds: float, reason: str) -> None:
        seconds = max(0.0, seconds)
        if seconds > self.max_wait:
            raise ResolveError(
                f"La espera requerida ({seconds:.0f}s) supera --max-wait "
                f"({self.max_wait:.0f}s)."
            )
        if seconds > 0:
            self._log(f"    esperando {seconds:.1f}s ({reason})...")
            time.sleep(seconds)

    def _import_browser_cookies(self, outcome: BrowserOutcome) -> None:
        for cookie in outcome.cookies:
            try:
                self.session.cookies.set(
                    cookie.get("name", ""),
                    cookie.get("value", ""),
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
            except Exception:
                continue

    def _run_browser(
        self,
        url: str,
        *,
        reason: str,
        actions: list[dict[str, object]] | None = None,
        speed_up_timers: bool = False,
        save: bool = False,
        output: str | None = None,
    ) -> BrowserOutcome | None:
        """Ejecuta el navegador headless una sola vez por URL."""
        if url in self._browser_attempted:
            return None
        self._browser_attempted.add(url)
        if not BrowserResolver.available():
            self._log(f"    {reason}; Playwright no está instalado")
            return None
        modo = "visible (debug)" if self.debug_browser else "headless"
        self._log(f"    {reason}; usando navegador {modo}...")
        try:
            outcome = BrowserResolver(
                timeout=self.timeout,
                max_wait=self.max_wait,
                verbose=self.verbose,
                user_agent=self.session.headers.get("User-Agent"),
                cookies=self.session.cookies,
                allow_private=self.allow_private,
                max_sequence_seconds=self.browser_timeout,
                maze=True,
                debug=self.debug_browser,
                slow_mo=250 if self.debug_browser else None,
            ).resolve(
                url,
                actions=actions,
                speed_up_timers=speed_up_timers,
                save=save,
                output=output,
                force=True,
                as_dir=True,
            )
        except Exception as exc:
            self._log(f"    el navegador headless falló: {exc}")
            return None
        self._import_browser_cookies(outcome)
        return outcome

    def _browser_rescue(self, url: str, reason: str) -> str | None:
        """Intenta superar un challenge anti-bot y devuelve la nueva URL."""
        if not self.browser_fallback:
            return None
        outcome = self._run_browser(url, reason=reason)
        if outcome is None:
            return None
        target = outcome.extracted_url or outcome.final_url
        if target and target != url:
            self._log(f"    navegador resolvió -> {redact_url(target)}")
            return target
        return None

    def _browser_plan_attempt(
        self,
        url: str,
        plan: list[dict[str, object]],
        *,
        reason: str = "flujo multi-click",
    ) -> "Resolution | str | None":
        """Ejecuta un plan de acciones y devuelve Resolution, URL o None."""
        tempdir = Path(tempfile.mkdtemp(prefix="desenlace-browser-"))
        outcome = self._run_browser(
            url,
            reason=reason,
            actions=plan,
            speed_up_timers=True,
            save=True,
            output=str(tempdir),
        )
        if outcome is None:
            shutil.rmtree(tempdir, ignore_errors=True)
            return None

        if outcome.destination is not None:
            filename = sanitize_filename(
                outcome.filename or outcome.destination.name or "download.bin"
            )
            self._log(f"    descarga capturada por el navegador: {filename}")
            return Resolution(
                final_url=outcome.final_url or url,
                filename=filename,
                referer=url,
                hops=1,
                page_url=url,
                prefetched=outcome.destination,
            )

        shutil.rmtree(tempdir, ignore_errors=True)
        if outcome.timed_out:
            self._log("    el flujo multi-click agotó el tiempo máximo")
        target = outcome.extracted_url or outcome.download_url
        if not target:
            candidate = outcome.final_url
            if candidate and candidate != url and "wp-" not in candidate.lower():
                target = candidate
        if (
            target
            and target.startswith(("http://", "https://"))
            and not target.startswith("blob:")
            and "wp-" not in target.lower()
        ):
            self._log(f"    el navegador obtuvo -> {redact_url(target)}")
            return target
        return None

    def _prepare_start(self, url: str) -> str:
        # Con fallback de navegador dejamos el wrapper intacto: su botón
        # #mainButton inicia el flujo del acortador y hay que pulsarlo.
        if self.browser_fallback:
            return normalize_url(url)
        target = extract_wrapper_target(url)
        if target is None:
            return normalize_url(url)
        if not self.full_chain:
            self._log(f"Wrapper detectado; usando destino directo: {redact_url(target)}")
            return normalize_url(target)
        parts = urlsplit(url)
        api_url = (
            f"{parts.scheme}://{parts.netloc}/api/shorten"
            f"?url={quote(target, safe='')}"
        )
        self._log(f"Modo cadena completa; consultando {redact_url(api_url)}")
        response = self._request("GET", api_url, stream=False)
        try:
            data = response.json()
        except ValueError as exc:
            raise ResolveError("El wrapper no devolvió JSON.") from exc
        finally:
            response.close()
        shortened = data.get("shortenedUrl")
        if not shortened:
            raise ResolveError(f"El wrapper no devolvió shortenedUrl: {data}")
        return normalize_url(str(shortened))

    def resolve(self, start_url: str, *, use_cache: bool = True) -> Resolution:
        key = cache_key(start_url)
        if use_cache and self.cache is not None:
            entry = self.cache.get(key)
            if entry and entry.get("page_url"):
                page_url = str(entry["page_url"])
                self._log(f"    caché encontrada; validando {redact_url(page_url)}")
                try:
                    result = self._resolve_from(page_url)
                except Exception as exc:
                    # La caché ya apunta a la página final (p. ej. un artículo
                    # de la red de anuncios). Si no se puede "resolver" más,
                    # se usa directamente como destino para descargarla.
                    self._log(
                        f"    caché válida como destino final ({exc}); "
                        "descargando la página cacheada"
                    )
                    nombre = sanitize_filename(
                        str(entry.get("filename") or "")
                        or filename_from_url(page_url)
                        or "download.html"
                    )
                    return Resolution(
                        final_url=page_url,
                        filename=nombre,
                        referer=start_url,
                        hops=0,
                        page_url=page_url,
                    )
                else:
                    if result.page_url is None:
                        result.page_url = page_url
                    if self.cache is not None:
                        self.cache.put(key, result.page_url, result.filename)
                    return result

        result = self._resolve_from(start_url)
        if self.cache is not None and result.page_url:
            self.cache.put(key, result.page_url, result.filename)
        return result

    def _resolve_from(self, start_url: str) -> Resolution:
        url = self._prepare_start(start_url.strip())
        method = "GET"
        data: dict[str, str] | None = None
        referer: str | None = None
        visit_counts: dict[tuple[str, str], int] = {}

        for _ in range(1, self.max_hops + 1):
            key = (method, url)
            count = visit_counts.get(key, 0)
            if count >= 3:
                raise ResolveError(f"Bucle detectado en {redact_url(url)}.")
            visit_counts[key] = count + 1

            headers: dict[str, str] = {}
            if referer:
                headers["Referer"] = referer
            response = self._request(method, url, data=data, headers=headers, stream=True)
            response_url = response.url
            status = response.status_code

            self._log(
                f"[{len(visit_counts)}] {method} {redact_url(url)} -> "
                f"{status} {redact_url(response_url)}"
            )
            self.trace.append(
                {
                    "hop": len(visit_counts),
                    "method": method,
                    "url": redact_url(url),
                    "status": status,
                    "final_url": redact_url(response_url),
                }
            )
            for redirect in response.history:
                self._log(f"    {redirect.status_code} {redact_url(redirect.url)}")
            if self.verbose:
                content_type = response.headers.get("content-type", "?")
                self._log(f"    content-type: {content_type}")

            if is_html_response(response):
                redirect_target = extract_redirect_target(response_url)
                if redirect_target is not None:
                    self._log(
                        f"    redirector detectado; destino directo: "
                        f"{redact_url(redirect_target)}"
                    )
                    response.close()
                    method, url, data, referer = (
                        "GET",
                        redirect_target,
                        None,
                        response_url,
                    )
                    continue

            if status >= 400:
                html = response.text[:1000] if is_html_response(response) else ""
                response.close()
                if is_challenge(status, html):
                    rescued = self._browser_rescue(response_url, f"HTTP {status}")
                    if rescued is not None:
                        method, url, data, referer = (
                            "GET",
                            rescued,
                            None,
                            response_url,
                        )
                        continue
                    raise ResolveError(
                        "Cloudflare/captcha bloqueó la petición y no se pudo "
                        "superar con el navegador. Instala Playwright "
                        "(`pip install playwright && playwright install chromium`) "
                        "o usa --cookies con una sesión válida."
                    )
                raise ResolveError(f"HTTP {status} en {redact_url(response_url)}")

            # Archivo directo (no HTML)
            if not is_html_response(response):
                filename = (
                    filename_from_disposition(
                        response.headers.get("content-disposition")
                    )
                    or filename_from_url(response_url)
                    or "download.bin"
                )
                filename = ensure_extension(
                    filename, response.headers.get("content-type")
                )
                result = Resolution(
                    final_url=response_url,
                    filename=sanitize_filename(filename),
                    referer=referer or response_url,
                    hops=len(visit_counts),
                )
                response.close()
                return result

            html = response.text
            low_html = html.lower()
            if self.browser_fallback and (
                "select_number" in low_html
                or "selecciona el n" in low_html
                or "ic-challenges.js" in low_html
            ):
                self._log("    captcha numérico detectado; usando navegador")
                response.close()
                outcome = self._browser_plan_attempt(
                    response_url, [], reason="captcha numérico"
                )
                if isinstance(outcome, Resolution):
                    return outcome
                if outcome:
                    method, url, data, referer = "GET", outcome, None, response_url
                    continue
            action = detect_action(html, response_url)
            if self.trace:
                self.trace[-1]["action"] = action.kind if action is not None else None
                self.trace[-1]["plugin"] = getattr(action, "plugin", None) if action else None
            if action is not None and self.verbose:
                self._log(f"    acción detectada: {action.kind}")
            if action is None:
                if "too early" in html.lower() and visit_counts[key] < 3:
                    self._log("    página 'Too Early'; reintentando en 3 s...")
                    response.close()
                    time.sleep(3)
                    method, url, data = "GET", response_url, None
                    continue

                if is_bot_challenge(status, html):
                    response.close()
                    rescued = self._browser_rescue(response_url, "página anti-bot")
                    if rescued is not None:
                        method, url, data, referer = (
                            "GET",
                            rescued,
                            None,
                            response_url,
                        )
                        continue
                    raise ResolveError(
                        "La página pide verificación anti-bot y el navegador "
                        f"headless no pudo resolverla: {redact_url(response_url)}"
                    )

                if not looks_like_shortener(html, response_url):
                    # Última oportunidad: páginas de descarga con varios clics
                    # que no delatan su naturaleza con los marcadores habituales.
                    if self.browser_fallback:
                        candidate_plan = build_browser_plan(html, response_url)
                        if candidate_plan:
                            self._log("    plan multi-click detectado (página genérica)")
                            response.close()
                            outcome = self._browser_plan_attempt(
                                response_url, candidate_plan
                            )
                            if isinstance(outcome, Resolution):
                                return outcome
                            if outcome:
                                method, url, data, referer = (
                                    "GET",
                                    outcome,
                                    None,
                                    response_url,
                                )
                                continue
                            self._log(
                                "    el plan no obtuvo nada; se trata como página final"
                            )
                            return Resolution(
                                final_url=response_url,
                                filename=sanitize_filename(
                                    filename_from_url(response_url) or "download.bin"
                                ),
                                referer=referer or response_url,
                                hops=len(visit_counts),
                                page_url=response_url,
                            )
                    self._log("    página final detectada (sin más pasos de acortador)")
                    result = Resolution(
                        final_url=response_url,
                        filename=sanitize_filename(
                            filename_from_url(response_url) or "download.bin"
                        ),
                        referer=referer or response_url,
                        hops=len(visit_counts),
                        page_url=response_url,
                    )
                    response.close()
                    return result

                plan = build_browser_plan(html, response_url)
                if plan and self.browser_fallback:
                    self._log("    plan multi-click detectado")
                    response.close()
                    outcome = self._browser_plan_attempt(response_url, plan)
                    if isinstance(outcome, Resolution):
                        return outcome
                    if outcome:
                        method, url, data, referer = "GET", outcome, None, response_url
                        continue

                title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
                title = title_match.group(1).strip() if title_match else "sin título"
                response.close()
                rescued = self._browser_rescue(
                    response_url, "página de acortador no reconocida"
                )
                if rescued is not None:
                    method, url, data, referer = "GET", rescued, None, response_url
                    continue
                raise ResolveError(
                    f"No se reconoce la página en {redact_url(response_url)} "
                    f"(título: {title!r}). Si requiere varios clics, usa "
                    "--auto-browser."
                )

            response.close()
            kind = action.kind
            if kind == "post_form":
                self._log(f"    formulario autoenviado -> {redact_url(action.url)}")
                method, url, data, referer = "POST", action.url, action.fields, response_url
            elif kind == "wait_post_form":
                self._wait(action.delay + self.wait_buffer, "verificación del acortador")
                method, url, data, referer = "POST", action.url, action.fields, response_url
            elif kind == "wait_get":
                self._wait(action.delay + self.wait_buffer, "verificación del acortador")
                method, url, data, referer = "GET", action.url, None, response_url
            elif kind == "goto":
                method, url, data, referer = "GET", action.url, None, response_url
            elif kind == "xhr_post":
                self._wait(action.delay + self.wait_buffer, "contador de AdLinkFly")
                xhr_headers = {
                    "Referer": response_url,
                    "Origin": origin_of(response_url),
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                }
                xhr = self._request(
                    "POST", action.url, data=action.fields, headers=xhr_headers, stream=False
                )
                try:
                    xhr_status = xhr.status_code
                    xhr_text = xhr.text
                finally:
                    xhr.close()
                if xhr_status >= 400:
                    raise ResolveError(
                        f"HTTP {xhr_status} en {redact_url(action.url)}"
                    )
                try:
                    payload = json.loads(xhr_text)
                except ValueError:
                    payload = None
                next_url = next_url_from_payload(payload)
                if not next_url:
                    # Algunos clones devuelven la URL en texto plano o HTML.
                    found = URL_IN_TEXT_RE.search(xhr_text)
                    if found and is_plausible_url(found.group(0)):
                        next_url = found.group(0)
                if not next_url:
                    message = ""
                    if isinstance(payload, dict):
                        message = str(
                            payload.get("message")
                            or payload.get("msg")
                            or payload.get("error")
                            or ""
                        )
                    raise ResolveError(
                        "El servidor no devolvió URL: "
                        f"{message or xhr_text[:200]!r}"
                    )
                self._log("    enlace final obtenido del contador")
                method, url, data, referer = "GET", urljoin(response_url, str(next_url)), None, response_url
            elif kind == "browser":
                self._log("    el plugin solicita un flujo con navegador headless")
                outcome = self._browser_plan_attempt(
                    action.url or response_url,
                    action.plan,
                    reason="flujo de plugin (navegador)",
                )
                if isinstance(outcome, Resolution):
                    return outcome
                if outcome:
                    method, url, data, referer = "GET", outcome, None, response_url
                    continue
                raise ResolveError(
                    "El flujo con navegador no obtuvo ningún enlace en "
                    f"{redact_url(response_url)}."
                )
            elif kind == "telegram_bot":
                self._log("    resolviendo bot de Telegram...")
                from telegram_client import resolve_bot_link

                telegram = resolve_bot_link(
                    action.url,
                    timeout=max(30.0, self.max_wait),
                    save=True,
                )
                if telegram.file_path is not None:
                    return Resolution(
                        final_url=telegram.message_url or action.url,
                        filename=sanitize_filename(
                            telegram.filename or telegram.file_path.name
                        ),
                        referer=response_url,
                        hops=len(visit_counts),
                        page_url=response_url,
                        prefetched=telegram.file_path,
                    )
                if telegram.final_url:
                    method, url, data, referer = (
                        "GET",
                        telegram.final_url,
                        None,
                        response_url,
                    )
                    continue
                raise ResolveError(
                    "Telegram no devolvió ni archivo ni enlace utilizable."
                )
            elif kind == "mediafire":
                self._log("    enlace directo de descarga detectado")
                result = Resolution(
                    final_url=action.url,
                    filename=sanitize_filename(
                        action.filename or filename_from_url(action.url) or "download.bin"
                    ),
                    referer=response_url,
                    hops=len(visit_counts),
                    page_url=response_url,
                )
                return result
            else:
                raise ResolveError(f"Acción no soportada: {kind}")

        raise ResolveError(f"Se superó el límite de {self.max_hops} saltos.")

    def download(
        self,
        target: Resolution,
        output: str | None = None,
        *,
        force: bool = False,
        as_dir: bool = True,
        progress: Callable[[int, int], None] | None = None,
        max_size: int | None = None,
    ) -> Path:
        """Descarga el archivo final (la lógica vive en downloader.py)."""
        return download_resolution(
            self.session,
            target,
            output,
            timeout=self.timeout,
            force=force,
            as_dir=as_dir,
            progress=progress,
            log=self._log,
            max_redirects=self.max_redirects,
            resume_retries=self.retries,
            max_size=max_size if max_size is not None else self.max_size,
        )


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
    resolver: LinkResolver | None = None,
    max_sequence_seconds: float = 120.0,
    allow_private: bool | None = None,
    actions: Any = None,
    speed_up_timers: bool = False,
    maze: bool = True,
    debug: bool = False,
    slow_mo: int | None = None,
) -> tuple[str, str, Path | None]:
    """Wrapper de compatibilidad sobre :mod:`browser_resolver`."""
    if resolver is None:
        resolver = LinkResolver(
            timeout=timeout,
            max_wait=max_wait,
            verbose=verbose,
            user_agent=user_agent,
        )
    return _browser_resolve_and_download(
        start_url,
        output,
        force=force,
        as_dir=as_dir,
        timeout=timeout,
        max_wait=max_wait,
        verbose=verbose,
        save=save,
        user_agent=user_agent,
        resolver=resolver,
        allow_private=(
            getattr(resolver, "allow_private", False)
            if allow_private is None
            else allow_private
        ),
        max_sequence_seconds=max_sequence_seconds,
        actions=actions,
        speed_up_timers=speed_up_timers,
        maze=maze,
        debug=debug,
        slow_mo=slow_mo,
    )
