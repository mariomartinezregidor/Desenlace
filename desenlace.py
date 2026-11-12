#!/usr/bin/env python3
"""Desenlace: resuelve cadenas de redirección con esperas y publicidad, y descarga el archivo final.

El motor usa un sistema de plugins (carpeta ``plugins/``), valida cada URL
contra SSRF, limita las redirecciones y puede apoyarse en un navegador
headless (``browser_resolver.py``) cuando aparece un challenge anti-bot.

Ejemplos:
    python3 desenlace.py "https://web-api-13.vercel.app/?url=https://softurl.in/XXXX"
    python3 desenlace.py "https://softurl.in/XXXX" --info
    python3 desenlace.py "https://softurl.in/XXXX" -o mis_descargas
    python3 desenlace.py --cookies cookies.txt "https://ouo.io/XXXX"
    python3 desenlace.py --auto-browser "https://sitio-con-cloudflare/XXXX"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import (
    parse_qsl,
    quote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

import requests

from desenlace_core import (
    BASE_DIR,
    CACHE_FILE,
    DEFAULT_CACHE_TTL,
    DEFAULT_MAX_HOPS,
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_MAX_WAIT,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DEFAULT_UA,
    DEFAULT_WAIT_BUFFER,
    DOWNLOAD_DIR,
    TRANSIENT_STATUS,
    URL_IN_TEXT_RE,
    Action,
    PageContext,
    ResolveError,
    UnsafeUrlError,
    absolute_url,
    base_destination,
    decode_adfly_ysmm,
    decode_embedded_url,
    decode_safelink,
    ensure_extension,
    extract_wrapper_target,
    filename_from_disposition,
    filename_from_url,
    first_js_url,
    form_action,
    form_fields,
    host_matches,
    is_plausible_url,
    is_redirect_form,
    js_unescape,
    next_url_from_payload,
    normalized_host,
    origin_of,
    parse_counter,
    release_destination,
    reserve_destination,
    resolve_js_target,
    safelink_delay,
    safelink_target,
    sanitize_filename,
    unique_path,
    unpack_javascript,
    validate_url,
)
import plugins
from browser_resolver import (
    BrowserOutcome,
    BrowserResolver,
    browser_resolve_and_download as _browser_resolve_and_download,
    is_bot_challenge,
)
from plugins import detect_action, looks_like_shortener, normalize_url
from plugins.file_hosts import normalize_file_host_url
from plugins.redirectors import (
    extract_redirect_target,
    extract_search_redirect_target,
)

# Compatibilidad: nombre histórico del detector de challenges.
is_challenge = is_bot_challenge

# Catálogo agregado de los plugins (lo usan tests y herramientas externas).
SHORTENER_HOSTS = plugins.SHORTENER_HOSTS
SHORTENER_MARKERS = plugins.SHORTENER_MARKERS
PLUGINS = plugins.PLUGINS

__all__ = [
    "BASE_DIR",
    "DOWNLOAD_DIR",
    "Action",
    "BrowserOutcome",
    "BrowserResolver",
    "PageContext",
    "Resolution",
    "ResolutionCache",
    "ResolveError",
    "UnsafeUrlError",
    "absolute_url",
    "browser_resolve_and_download",
    "build_parser",
    "decode_adfly_ysmm",
    "decode_embedded_url",
    "decode_safelink",
    "detect_action",
    "extract_redirect_target",
    "extract_search_redirect_target",
    "extract_wrapper_target",
    "first_js_url",
    "form_action",
    "form_fields",
    "host_matches",
    "is_bot_challenge",
    "is_challenge",
    "is_redirect_form",
    "js_unescape",
    "load_cookie_file",
    "looks_like_shortener",
    "main",
    "normalize_file_host_url",
    "normalize_url",
    "normalized_host",
    "parse_counter",
    "parse_extra_headers",
    "resolve_js_target",
    "safelink_delay",
    "safelink_target",
    "unique_path",
    "unpack_javascript",
    "validate_url",
]

@dataclass
class Resolution:
    final_url: str
    filename: str
    referer: str
    hops: int
    page_url: str | None = None


@dataclass
class JobResult:
    index: int
    start_url: str
    resolution: Resolution | None = None
    destination: Path | None = None
    skipped: bool = False
    error: Exception | None = None
    elapsed: float = 0.0


def read_urls_file(path: str | Path) -> list[str]:
    """Lee URLs de un archivo de texto, una por línea (o separadas por espacios)."""
    file_path = Path(path)
    urls: list[str] = []
    with file_path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for token in line.split():
                if not token.startswith(("http://", "https://")):
                    raise ResolveError(
                        f"URL inválida en {file_path}:{line_number}: {token!r}"
                    )
                urls.append(token)
    if not urls:
        raise ResolveError(f"No se encontraron URLs en {file_path}.")
    return urls


def dedupe(urls: list[str]) -> list[str]:
    """Elimina enlaces repetidos conservando el orden."""
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def collect_urls(positional: list[str], files: list[str] | None) -> list[str]:
    """Combina URLs pasadas por argumento con las de uno o varios archivos."""
    urls: list[str] = []
    for item in positional:
        candidate = Path(item).expanduser()
        if candidate.is_file():
            urls.extend(read_urls_file(candidate))
        elif item.startswith(("http://", "https://")):
            urls.append(item)
        else:
            raise ResolveError(f"No es una URL ni un archivo existente: {item}")
    for file_name in files or []:
        urls.extend(read_urls_file(file_name))
    urls = dedupe(urls)
    if not urls:
        raise ResolveError("No se indicó ninguna URL.")
    return urls


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


def redact_url(url: str, max_value: int = 24) -> str:
    """Acorta valores sensibles para los logs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    sensitive = re.compile(r"(token|key|sig|hash|auth|session|csrf|redirect|url)", re.I)
    pairs: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if sensitive.search(key) or len(value) > max_value:
            if len(value) > 12:
                value = f"{value[:6]}...{value[-4:]}"
            else:
                value = "..."
        pairs.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))


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


class SafeSession(requests.Session):
    """Sesión HTTP que valida SSRF en cada petición, incluidos los redirects.

    ``requests`` llama a ``Session.send`` una vez por cada salto de la cadena,
    así que validar aquí bloquea también redirecciones ocultas hacia la red
    interna.
    """

    def __init__(self, *, allow_private: bool = False) -> None:
        super().__init__()
        self.allow_private = allow_private

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        validate_url(request.url or "", allow_private=self.allow_private)
        return super().send(request, **kwargs)


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
    ) -> None:
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
        self.session = SafeSession(allow_private=allow_private)
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
        line = f"{self.prefix}{message}"
        if self.log_lock is None:
            print(line, file=sys.stderr, flush=True)
        else:
            with self.log_lock:
                print(line, file=sys.stderr, flush=True)

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

    def _browser_rescue(self, url: str, reason: str) -> str | None:
        """Intenta superar un challenge anti-bot con Playwright.

        Sólo se ejecuta si ``browser_fallback`` está activo y una vez por URL.
        Devuelve la nueva URL si el navegador avanzó; ``None`` en caso contrario.
        """
        if not self.browser_fallback:
            return None
        if url in self._browser_attempted:
            return None
        self._browser_attempted.add(url)
        if not BrowserResolver.available():
            self._log(
                f"    challenge anti-bot ({reason}); Playwright no está instalado"
            )
            return None
        self._log(f"    challenge anti-bot ({reason}); usando navegador headless...")
        try:
            outcome = BrowserResolver(
                timeout=self.timeout,
                max_wait=self.max_wait,
                verbose=self.verbose,
                user_agent=self.session.headers.get("User-Agent"),
                cookies=self.session.cookies,
                allow_private=self.allow_private,
            ).resolve(url)
        except Exception as exc:
            self._log(f"    el navegador headless falló: {exc}")
            return None

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
        if outcome.final_url and outcome.final_url != url:
            self._log(
                f"    navegador resolvió el challenge -> {redact_url(outcome.final_url)}"
            )
            return outcome.final_url
        return None

    def _prepare_start(self, url: str) -> str:
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
                    self._log(f"    caché no válida ({exc}); resolviendo de nuevo")
                    self.cache.remove(key)
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
            action = detect_action(html, response_url)
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
                    f"(título: {title!r})."
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
    ) -> Path:
        """Descarga el archivo final.

        Si se pasa ``progress``, se invoca con ``(descargado, total)`` en bytes
        (``total`` puede ser 0 si el servidor no envía ``content-length``).
        """
        headers = {
            "Referer": target.referer or target.final_url,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        destination: Path | None = None
        try:
            response = self.session.get(
                target.final_url,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=True,
                stream=True,
            )
        except requests.TooManyRedirects as exc:
            raise ResolveError(
                f"Se superó el límite de {self.max_redirects} redirecciones "
                f"al descargar {redact_url(target.final_url)}"
            ) from exc
        try:
            if response.status_code >= 400:
                raise ResolveError(
                    f"HTTP {response.status_code} al descargar "
                    f"{redact_url(target.final_url)}"
                )
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type:
                if target.page_url:
                    filename = sanitize_filename("enlace.url")
                    destination = self._destination(
                        output, filename, as_dir=as_dir, force=force
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(
                        f"[InternetShortcut]\nURL={target.final_url}\n",
                        encoding="utf-8",
                    )
                    self._log(
                        "    el destino final es HTML; guardado acceso directo .url"
                    )
                    return destination
                snippet = response.text[:300]
                raise ResolveError(
                    "El enlace de descarga devolvió HTML en vez del archivo "
                    f"(posible login o bloqueo): {snippet!r}"
                )

            disposition_name = filename_from_disposition(
                response.headers.get("content-disposition")
            )
            filename = ensure_extension(
                disposition_name
                or target.filename
                or filename_from_url(response.url)
                or "download.bin",
                response.headers.get("content-type"),
            )
            filename = sanitize_filename(filename)
            destination = self._destination(
                output, filename, as_dir=as_dir, force=force
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            part = Path(str(destination) + ".part")
            total = int(response.headers.get("content-length") or 0)
            downloaded = 0
            last_progress = 0.0
            if progress is not None:
                progress(0, total)

            try:
                with part.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if progress is not None:
                            progress(downloaded, total)
                        if (
                            sys.stderr.isatty()
                            and total
                            and now - last_progress >= 0.5
                        ):
                            last_progress = now
                            percent = min(100, downloaded * 100 // total)
                            print(
                                f"\r    descargando {percent:3d}% "
                                f"({downloaded}/{total} bytes)",
                                end="",
                                file=sys.stderr,
                                flush=True,
                            )
                if sys.stderr.isatty() and total:
                    print(file=sys.stderr)
                if total and downloaded != total:
                    raise ResolveError(
                        f"Descarga incompleta: {downloaded}/{total} bytes."
                    )
                part.replace(destination)
            except BaseException:
                part.unlink(missing_ok=True)
                raise

            self._log(f"    guardado {destination} ({downloaded} bytes)")
            return destination
        finally:
            release_destination(destination)
            response.close()

    def _destination(
        self, output: str | None, filename: str, *, as_dir: bool, force: bool
    ) -> Path:
        return reserve_destination(
            output, filename, as_dir=as_dir, force=force
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
        allow_private=getattr(resolver, "allow_private", False),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Resuelve cadenas de redirección con esperas/publicidad y descarga "
            "el archivo final."
        )
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="URL(s) de inicio o archivo(s) .txt con una URL por línea",
    )
    parser.add_argument(
        "-f",
        "--file",
        action="append",
        metavar="ARCHIVO",
        help="Archivo con URLs, una por línea. Se puede repetir.",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Solo mostrar el enlace final, sin descargar",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Archivo de destino o carpeta (por defecto ./downloads)",
    )
    parser.add_argument(
        "--full-chain",
        action="store_true",
        help="Seguir el wrapper /api/shorten en vez de extraer el destino directo",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Usar un navegador headless (Chromium) como fallback para webs complejas.",
    )
    parser.add_argument(
        "--auto-browser",
        action="store_true",
        help="Si el modo HTTP falla, reintentar automáticamente con navegador.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Sobrescribir archivos existentes",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Mostrar detalles adicionales",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Timeout por petición en segundos (por defecto {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=DEFAULT_MAX_WAIT,
        help=f"Espera máxima permitida por temporizador (por defecto {DEFAULT_MAX_WAIT:g})",
    )
    parser.add_argument(
        "--max-hops",
        type=int,
        default=DEFAULT_MAX_HOPS,
        help=f"Máximo de saltos (por defecto {DEFAULT_MAX_HOPS})",
    )
    parser.add_argument(
        "--max-redirects",
        type=int,
        default=DEFAULT_MAX_REDIRECTS,
        metavar="N",
        help=(
            "Límite estricto de redirecciones HTTP por petición "
            f"(por defecto {DEFAULT_MAX_REDIRECTS})"
        ),
    )
    parser.add_argument(
        "--allow-private",
        action="store_true",
        help=(
            "Permitir IPs privadas/loopback. Desactiva la protección SSRF: "
            "úsalo sólo en pruebas locales"
        ),
    )
    parser.add_argument(
        "--list-plugins",
        action="store_true",
        help="Mostrar los plugins cargados y salir",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=0,
        metavar="N",
        help="Enlaces resueltos en paralelo (0 = automático: hasta 4)",
    )
    parser.add_argument(
        "--wait-buffer",
        type=float,
        default=DEFAULT_WAIT_BUFFER,
        metavar="SEGUNDOS",
        help=(
            "Margen extra tras cada temporizador del servidor "
            f"(por defecto {DEFAULT_WAIT_BUFFER:g} s)"
        ),
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="No leer ni escribir la caché de resoluciones",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignorar la caché y resolver de nuevo (la actualiza)",
    )
    parser.add_argument(
        "--cache-ttl",
        type=float,
        default=168.0,
        metavar="HORAS",
        help="Validez de la caché en horas (por defecto 168)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="No descargar si el archivo ya existe con contenido",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        metavar="N",
        help=(
            "Reintentos ante errores de red o HTTP 429/5xx "
            f"(por defecto {DEFAULT_RETRIES})"
        ),
    )
    parser.add_argument(
        "--user-agent",
        metavar="UA",
        help="User-Agent a usar en las peticiones (por defecto uno de Chrome)",
    )
    parser.add_argument(
        "--cookies",
        metavar="ARCHIVO",
        help=(
            "Archivo de cookies (formato Netscape o 'nombre=valor') para "
            "sitios con Cloudflare o sesión iniciada"
        ),
    )
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        metavar="CABECERA",
        help="Cabecera HTTP extra, por ejemplo 'Authorization: Bearer ...'. Se puede repetir.",
    )
    return parser


def parse_extra_headers(items: list[str] | None) -> dict[str, str]:
    """Convierte `['Nombre: valor', ...]` en un diccionario de cabeceras."""
    headers: dict[str, str] = {}
    for item in items or []:
        if ":" not in item:
            raise ResolveError(f"Cabecera inválida (falta ':'): {item!r}")
        name, value = item.split(":", 1)
        name = name.strip()
        if not name:
            raise ResolveError(f"Cabecera inválida: {item!r}")
        headers[name] = value.strip()
    return headers


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_plugins:
        for plugin in PLUGINS:
            hosts = ", ".join(plugin.hosts) if plugin.hosts else "-"
            capabilities = []
            if plugin.detect is not None:
                capabilities.append("detect")
            if plugin.detect_url is not None:
                capabilities.append("url")
            if plugin.normalize is not None:
                capabilities.append("normalize")
            print(
                f"{plugin.priority:>3}  {plugin.name:<22} "
                f"[{'/'.join(capabilities) or 'catálogo'}] {hosts}"
            )
        return 0

    try:
        urls = collect_urls(args.urls, args.file)
    except (OSError, ResolveError) as exc:
        print(f"Error leyendo los enlaces: {exc}", file=sys.stderr)
        return 1

    as_dir = True
    if (
        args.output
        and len(urls) == 1
        and not str(args.output).endswith(("/", os.sep))
        and not Path(args.output).is_dir()
    ):
        as_dir = False

    jobs = args.jobs if args.jobs > 0 else (
        1 if len(urls) == 1 else min(4, len(urls))
    )
    jobs = max(1, min(jobs, len(urls)))

    cache = ResolutionCache(
        CACHE_FILE,
        ttl=max(0.0, args.cache_ttl) * 3600,
        enabled=not args.no_cache,
    )
    log_lock = threading.Lock()
    total = len(urls)

    try:
        extra_headers = parse_extra_headers(args.header)
    except ResolveError as exc:
        print(f"Error en las cabeceras: {exc}", file=sys.stderr)
        return 1

    shared_cookies = None
    if args.cookies:
        try:
            shared_cookies = load_cookie_file(args.cookies)
        except ResolveError as exc:
            print(f"Error en las cookies: {exc}", file=sys.stderr)
            return 1

    resolver_options: dict[str, Any] = {
        "timeout": args.timeout,
        "max_wait": args.max_wait,
        "max_hops": args.max_hops,
        "verbose": args.verbose,
        "full_chain": args.full_chain,
        "wait_buffer": args.wait_buffer,
        "cache": cache,
        "log_lock": log_lock,
        "user_agent": args.user_agent,
        "headers": extra_headers,
        "cookies": shared_cookies,
        "retries": args.retries,
        "max_redirects": args.max_redirects,
        "allow_private": args.allow_private,
        "browser_fallback": args.auto_browser,
    }

    def run_job(index: int, start_url: str) -> JobResult:
        prefix = f"[{index}/{total}] " if total > 1 else ""
        started = time.monotonic()
        resolver = LinkResolver(prefix=prefix, **resolver_options)

        def run_browser() -> JobResult:
            final_url, filename, destination = browser_resolve_and_download(
                start_url,
                args.output,
                force=args.force,
                as_dir=as_dir,
                timeout=args.timeout,
                max_wait=args.max_wait,
                verbose=args.verbose,
                user_agent=args.user_agent,
                resolver=resolver,
            )
            result = Resolution(
                final_url=final_url,
                filename=sanitize_filename(filename),
                referer=start_url,
                hops=0,
            )
            if args.info:
                return JobResult(
                    index,
                    start_url,
                    result,
                    elapsed=time.monotonic() - started,
                )
            if destination is None:
                destination = resolver.download(
                    result, args.output, force=args.force, as_dir=as_dir
                )
            return JobResult(
                index,
                start_url,
                result,
                destination,
                elapsed=time.monotonic() - started,
            )

        try:
            if args.browser:
                return run_browser()
            result = resolver.resolve(start_url, use_cache=not args.refresh)
            if args.info:
                return JobResult(
                    index,
                    start_url,
                    result,
                    elapsed=time.monotonic() - started,
                )
            if args.skip_existing and not args.force:
                candidate = base_destination(args.output, result.filename, as_dir)
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return JobResult(
                        index,
                        start_url,
                        result,
                        candidate,
                        skipped=True,
                        elapsed=time.monotonic() - started,
                    )
            destination = resolver.download(
                result, args.output, force=args.force, as_dir=as_dir
            )
            return JobResult(
                index,
                start_url,
                result,
                destination,
                elapsed=time.monotonic() - started,
            )
        except Exception as exc:
            if args.auto_browser and not args.browser:
                try:
                    return run_browser()
                except Exception as exc2:
                    return JobResult(
                        index,
                        start_url,
                        error=exc2,
                        elapsed=time.monotonic() - started,
                    )
            return JobResult(
                index,
                start_url,
                error=exc,
                elapsed=time.monotonic() - started,
            )

    try:
        if jobs == 1:
            results = [run_job(index, url) for index, url in enumerate(urls, 1)]
        else:
            with ThreadPoolExecutor(max_workers=jobs) as executor:
                futures = [
                    executor.submit(run_job, index, url)
                    for index, url in enumerate(urls, 1)
                ]
                results = [future.result() for future in futures]
    except KeyboardInterrupt:
        print("\nCancelado.", file=sys.stderr)
        return 130

    failures = 0
    for job in results:
        if job.error is not None:
            failures += 1
            print(
                f"Error con {redact_url(job.start_url)}: {job.error}",
                file=sys.stderr,
            )
            continue
        if job.resolution is None:
            failures += 1
            print(
                f"Error con {redact_url(job.start_url)}: sin resultado",
                file=sys.stderr,
            )
            continue
        if args.info:
            print(job.resolution.final_url)
            continue
        if job.skipped:
            print(f"Omitido (ya existe): {job.destination} ({job.elapsed:.1f}s)")
            continue
        size = job.destination.stat().st_size if job.destination else 0
        print(f"URL final: {job.resolution.final_url}")
        print(f"Archivo: {job.resolution.filename}")
        print(f"Descargado: {job.destination} ({size} bytes) ({job.elapsed:.1f}s)")

    return 1 if failures else 0



if __name__ == "__main__":
    raise SystemExit(main())
