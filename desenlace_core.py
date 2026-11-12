"""Primitivas compartidas por el motor Desenlace y sus plugins.

Aquí viven los tipos de datos, los helpers de URL/HTML y la validación de
seguridad (SSRF). Ni este módulo ni los plugins dependen de ``desenlace.py``:
así el registro de plugins puede cargarse sin imports circulares.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import posixpath
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import (
    parse_qs,
    unquote,
    unquote_plus,
    urlencode,
    urljoin,
    urlsplit,
)

from bs4 import Tag

# ---------------------------------------------------------------------------
# Ajustes generales
# ---------------------------------------------------------------------------

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_WAIT = 90.0
DEFAULT_MAX_HOPS = 25
DEFAULT_MAX_REDIRECTS = 10
DEFAULT_WAIT_BUFFER = 0.4
DEFAULT_CACHE_TTL = 7 * 24 * 3600
DEFAULT_RETRIES = 2
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / ".desenlace_cache.json"
DOWNLOAD_DIR = Path("downloads")

_DEST_LOCK = threading.Lock()
_RESERVED_PATHS: set[Path] = set()

WRAPPER_HOSTS = ("web-api-13.vercel.app", "web-api.vercel.app")

# Redirectores que envuelven el destino real en un parámetro de la URL.
REDIRECT_ENDPOINTS: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
    ("google.com", "/url", ("url", "q")),
    ("googleusercontent.com", None, ("url", "q")),
    ("bing.com", "/ck/a", ("u", "url")),
    ("duckduckgo.com", "/l", ("uddg", "u")),
    ("yahoo.com", None, ("done", "url", "u")),
    ("facebook.com", "/l.php", ("u",)),
    ("out.reddit.com", None, ("url",)),
    ("away.vk.com", "/away.php", ("to",)),
    ("t.umblr.com", "/redirect", ("z",)),
    ("linkedin.com", "/redir/redirect", ("url",)),
    ("l.instagram.com", None, ("u",)),
    ("yandex.ru", None, ("url", "u", "redir")),
    ("yandex.com", None, ("url", "u", "redir")),
    ("qwant.com", None, ("url",)),
    ("ecosia.org", None, ("url",)),
)

GENERIC_REDIRECT_PARAMS = (
    "url",
    "u",
    "q",
    "to",
    "target",
    "dest",
    "destination",
    "redirect",
    "redirect_url",
    "redirect_uri",
    "return",
    "return_to",
    "next",
    "continue",
    "link",
    "out",
    "goto",
    "go",
    "redir",
    "safelink",
)

GATEWAY_PATHS = (
    "/url",
    "/redirect",
    "/redir",
    "/out",
    "/exit",
    "/away",
    "/go",
    "/goto",
    "/link",
    "/l.php",
    "/click",
)

_ASSET_URL_RE = re.compile(
    r"\.(?:js|css|mjs|map|png|jpe?g|gif|svg|webp|ico|bmp|woff2?|ttf|eot|otf)"
    r"(?:\?|#|$)",
    re.I,
)
_BLOCKED_URL_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "facebook.com",
    "twitter.com",
    "pinterest.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "gstatic.com",
    "cloudflare.com",
    "cdnjs.cloudflare.com",
    "jsdelivr.net",
    "unpkg.com",
    "bootstrapcdn.com",
    "jquery.com",
    "w3.org",
    "schema.org",
    "sentry.io",
    "hotjar.com",
    "newrelic.com",
)
_BLOCKED_URL_FRAGMENTS = (
    "javascript:",
    "about:blank",
    "mailto:",
    "tel:",
    "whatsapp:",
    "intent://",
)
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>\\]+")

JS_REDIRECT_PATTERNS = (
    r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
    r'window\.location\.replace\(\s*["\']([^"\']+)["\']\s*\)',
    r'window\.location\.assign\(\s*["\']([^"\']+)["\']\s*\)',
    r'location\.href\s*=\s*["\']([^"\']+)["\']',
    r'location\.replace\(\s*["\']([^"\']+)["\']\s*\)',
    r'location\.assign\(\s*["\']([^"\']+)["\']\s*\)',
    r'top\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
    r'document\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
    r'self\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
)

KNOWN_REDIRECT_FIELDS = {
    "go",
    "op",
    "id",
    "token",
    "url",
    "next",
    "continue",
    "step",
    "ad_form_data",
    "_csrftoken",
    "newwpsafelink",
    "safelink",
    "download",
    "download2",
    "confirm",
    "uuid",
}

CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/x-rar-compressed": ".rar",
    "application/x-7z-compressed": ".7z",
    "application/x-tar": ".tar",
    "application/gzip": ".gz",
    "application/epub+zip": ".epub",
    "application/vnd.android.package-archive": ".apk",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/octet-stream": ".bin",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "text/plain": ".txt",
}


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------


class ResolveError(RuntimeError):
    """Error controlado durante la resolución o la descarga."""


class UnsafeUrlError(ResolveError):
    """La URL apunta a un destino bloqueado por la política SSRF."""


@dataclass
class Action:
    kind: str
    url: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    filename: str = ""
    action_url: str = ""
    plan: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PageContext:
    """Datos compartidos por los detectores de una misma página."""

    html: str
    base_url: str
    soup: Any
    is_shortener: bool
    _counter: float | None = None

    @property
    def counter(self) -> float:
        if self._counter is None:
            self._counter = parse_counter(self.html)
        return self._counter


# ---------------------------------------------------------------------------
# Seguridad: validación SSRF
# ---------------------------------------------------------------------------

_METADATA_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.google",
        "instance-data",
    }
)
_DNS_CACHE: dict[tuple[str, int], tuple[float, tuple[str, ...] | None]] = {}
_DNS_LOCK = threading.Lock()
_DNS_TTL = 300.0


def normalized_host(url_or_host: str) -> str:
    """Devuelve el host en minúsculas y sin puerto."""
    value = (url_or_host or "").strip().lower()
    if "://" in value:
        try:
            value = urlsplit(value).netloc
        except ValueError:
            return ""
    return value.split(":")[0].strip("[]").rstrip(".")


def _is_blocked_ip(address: ipaddress._BaseAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return not address.is_global


def _resolve_host(host: str, port: int) -> tuple[str, ...] | None:
    key = (host, port)
    now = time.time()
    with _DNS_LOCK:
        cached = _DNS_CACHE.get(key)
        if cached and now - cached[0] < _DNS_TTL:
            return cached[1]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        addresses: tuple[str, ...] | None = None
    else:
        seen: list[str] = []
        for info in infos:
            address = info[4][0]
            if address not in seen:
                seen.append(address)
        addresses = tuple(seen)
    with _DNS_LOCK:
        _DNS_CACHE[key] = (now, addresses)
    return addresses


def validate_url(url: str, *, allow_private: bool = False) -> None:
    """Valida que una URL sea http(s) y no apunte a una red interna.

    Se comprueba en cada petición (incluidos los redirects) para evitar SSRF
    clásico: loopback, redes privadas, link-local y el endpoint de metadatos
    de las nubes (169.254.169.254). Si ``allow_private`` es True sólo se
    validan el esquema y el host (útil para tests y servidores locales).
    """
    if not url or not isinstance(url, str):
        raise UnsafeUrlError("URL vacía o no válida.")
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise UnsafeUrlError(f"URL no válida: {exc}") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise UnsafeUrlError(
            f"Esquema no permitido: {scheme or '(vacío)'}. Sólo http/https."
        )
    if not parts.hostname:
        raise UnsafeUrlError("La URL no tiene host.")
    if parts.username or parts.password:
        raise UnsafeUrlError("Las URLs con credenciales embebidas no están permitidas.")

    host = parts.hostname.lower().rstrip(".")
    if host in _METADATA_HOSTS or host.endswith(".internal"):
        raise UnsafeUrlError(f"Host de metadatos bloqueado: {host}")
    if allow_private:
        return

    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeUrlError(f"Puerto no válido: {exc}") from exc

    addresses: tuple[str, ...] | None
    literal = host.strip("[]")
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        address = None

    if address is not None:
        if _is_blocked_ip(address):
            raise UnsafeUrlError(f"Dirección IP no pública bloqueada: {literal}")
        return

    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise UnsafeUrlError("El host local está bloqueado por SSRF.")

    addresses = _resolve_host(host, port)
    if not addresses:
        raise UnsafeUrlError(f"No se pudo resolver el host {host!r}.")
    for raw in addresses:
        try:
            parsed = ipaddress.ip_address(raw)
        except ValueError:
            raise UnsafeUrlError(f"El host {host!r} resolvió a una IP no válida.")
        if _is_blocked_ip(parsed):
            raise UnsafeUrlError(
                f"El host {host!r} resuelve a una dirección no pública ({raw})."
            )


# ---------------------------------------------------------------------------
# Hosts y URLs
# ---------------------------------------------------------------------------


def host_matches(host: str, domains: Iterable[str]) -> bool:
    """Compara un host con dominios respetando los límites de etiqueta."""
    host = normalized_host(host)
    if not host:
        return False
    for domain in domains:
        domain = (domain or "").strip().lower().rstrip(".")
        if not domain:
            continue
        if domain.startswith("www."):
            domain = domain[4:]
        if domain.startswith("*."):
            domain = domain[2:]
        if host == domain or host.endswith("." + domain):
            return True
    return False


def is_plausible_url(url: str) -> bool:
    """Descarta recursos estáticos, redes sociales y esquemas no descargables."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    low = url.lower()
    if any(fragment in low for fragment in _BLOCKED_URL_FRAGMENTS):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if not parts.netloc:
        return False
    if host_matches(parts.netloc, _BLOCKED_URL_HOSTS):
        return False
    if _ASSET_URL_RE.search(parts.path):
        return False
    return True


def decode_embedded_url(value: str | None) -> str | None:
    """Extrae la primera URL http(s) escondida en un valor (texto/base64/JSON)."""
    if not value:
        return None
    candidates: list[str] = []
    current = value.strip().strip("'\"")
    candidates.append(current)
    for _ in range(3):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
        candidates.append(current)
    for candidate in list(candidates):
        compact = re.sub(r"\s+", "", candidate)
        for offset in (0, 1, 2, 3):
            chunk = compact[offset:]
            if len(chunk) < 8:
                continue
            padded = chunk + "=" * (-len(chunk) % 4)
            for decoder in (base64.urlsafe_b64decode, base64.b64decode):
                try:
                    raw = decoder(padded)
                    text = raw.decode("utf-8", "ignore")
                except Exception:
                    continue
                candidates.append(text)
    for candidate in candidates:
        match = _URL_IN_TEXT_RE.search(candidate)
        if match:
            target = match.group(0).rstrip(").,;\"'")
            if is_plausible_url(target):
                return target
    return None


def extract_wrapper_target(url: str) -> str | None:
    """Extrae el parámetro ?url= de wrappers alojados en Vercel."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.query:
        return None
    host = normalized_host(parts.netloc)
    if not host_matches(host, ("vercel.app",)):
        return None
    values = parse_qs(parts.query).get("url")
    if not values:
        return None
    target = values[0].strip()
    if not target.startswith(("http://", "https://")):
        return None
    return target


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def absolute_url(base_url: str, href: str | None) -> str | None:
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    try:
        return urljoin(base_url, href)
    except ValueError:
        return None


def form_action(
    form: Tag, fields: dict[str, str], base_url: str, kind: str = "post_form"
) -> Action:
    """Construye la acción de un formulario respetando su método."""
    action = (form.get("action") or "").strip()
    if not action or action.startswith(("javascript:", "#")):
        action = base_url
    action_url = urljoin(base_url, action)
    method = (form.get("method") or "POST").upper()
    if method == "GET" and fields:
        query = urlencode(fields)
        separator = "&" if "?" in action_url else "?"
        return Action("goto", url=f"{action_url}{separator}{query}")
    return Action(kind, url=action_url, fields=fields)


def is_redirect_form(form: Tag, fields: dict[str, str]) -> bool:
    if not fields:
        return False
    inputs = form.find_all("input")
    all_hidden = bool(inputs) and all(
        (inp.get("type") or "text").lower() == "hidden" for inp in inputs
    )
    known = any(name.lower() in KNOWN_REDIRECT_FIELDS for name in fields)
    return all_hidden or known


# ---------------------------------------------------------------------------
# Nombres de archivo y destinos
# ---------------------------------------------------------------------------


def sanitize_filename(name: str, max_len: int = 180) -> str:
    name = (name or "").strip().strip('"').strip()
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return "download.bin"
    if len(name) > max_len:
        stem, suffix = os.path.splitext(name)
        keep = max(1, max_len - len(suffix))
        name = stem[:keep] + suffix
    return name


def filename_from_url(url: str) -> str | None:
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    base = posixpath.basename(path.rstrip("/"))
    if not base:
        return None
    return unquote_plus(base)


def filename_from_disposition(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)?''([^;]+)", value, re.I)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename\s*=\s*"([^"]+)"', value, re.I)
    if match:
        return match.group(1).strip()
    match = re.search(r"filename\s*=\s*([^;]+)", value, re.I)
    if match:
        return match.group(1).strip().strip('"')
    return None


def ensure_extension(name: str, content_type: str | None) -> str:
    """Añade la extensión coherente con el Content-Type si falta."""
    if not name or os.path.splitext(name)[1]:
        return name
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    extension = CONTENT_TYPE_EXTENSIONS.get(mime)
    return f"{name}{extension}" if extension else name


def base_destination(output: str | None, filename: str, as_dir: bool) -> Path:
    """Ruta de destino sin resolver nombres duplicados."""
    if output is None:
        return DOWNLOAD_DIR / filename
    base = Path(output).expanduser()
    if as_dir or str(output).endswith(("/", os.sep)) or base.is_dir():
        return base / filename
    return base


def unique_path(path: Path, avoid: set[Path] | None = None) -> Path:
    avoid = avoid or set()

    def available(candidate: Path) -> bool:
        return not candidate.exists() and candidate not in avoid

    if available(path):
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(1, 10000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if available(candidate):
            return candidate
    return path.with_name(f"{stem}-{int(time.time())}{suffix}")


def reserve_destination(
    output: str | None, filename: str, *, as_dir: bool, force: bool
) -> Path:
    """Reserva un destino único (thread-safe) para una descarga."""
    with _DEST_LOCK:
        destination = base_destination(output, filename, as_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination in _RESERVED_PATHS or (
            destination.exists() and not force
        ):
            destination = unique_path(destination, _RESERVED_PATHS)
        _RESERVED_PATHS.add(destination)
        return destination


def release_destination(destination: Path | None) -> None:
    if destination is None:
        return
    with _DEST_LOCK:
        _RESERVED_PATHS.discard(destination)


# ---------------------------------------------------------------------------
# Parsers de páginas de espera
# ---------------------------------------------------------------------------

COUNTER_PATTERNS = (
    r'"counter_value"\s*:\s*"?([\d.]+)',
    r'"delay"\s*:\s*"?([\d.]+)',
    r'"countdown"\s*:\s*"?([\d.]+)',
    r'(?:var|let|const)\s+\w*(?:counter|countdown|timer|wait|delay)\w*\s*=\s*["\']?([\d.]+)',
    r'id=["\'](?:custom-timer|timer|countdown|countdown-timer|wait-timer)["\'][^>]*>\s*([\d.]+)',
    r'data-(?:timer|seconds|countdown|delay|wait)\s*=\s*["\']?([\d.]+)',
)
COUNTER_RE = tuple(re.compile(pattern, re.I) for pattern in COUNTER_PATTERNS)


def parse_counter(html: str) -> float:
    """Busca el temporizador que impone el acortador antes de continuar."""
    for pattern in COUNTER_RE:
        match = pattern.search(html)
        if not match:
            continue
        try:
            return max(0.0, float(match.group(1)))
        except (TypeError, ValueError):
            continue
    return 1.0


def decode_safelink(value: str) -> dict:
    """Decodifica el payload base64 (o JSON directo) de wp-safelink."""
    text = unquote_plus(unquote(value.strip()))
    candidates = [text]
    if text.startswith("{"):
        candidates.insert(0, text)
    for candidate in candidates:
        padded = candidate + "=" * (-len(candidate) % 4)
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                raw = decoder(padded)
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                return data
    raise ResolveError("No se pudo decodificar el payload newwpsafelink.")


def safelink_target(payload: dict) -> str | None:
    for key in ("linkr", "url", "redirect", "link", "destination", "go"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            candidate = decode_embedded_url(value) or value.strip()
            if candidate.startswith(("http://", "https://", "/")):
                return candidate
    return None


def safelink_delay(payload: dict, default: float = 25.0) -> float:
    for key in ("delay", "countdown", "timer", "wait", "seconds"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return default


NEXT_URL_KEYS = ("url", "link", "redirect", "destination", "next", "result", "href")


def next_url_from_payload(payload: Any) -> str | None:
    """Busca la URL de continuación en las distintas respuestas JSON conocidas."""
    if not isinstance(payload, dict):
        return None
    for key in NEXT_URL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            target = decode_embedded_url(value) or value.strip()
            if target.startswith(("http://", "https://", "/")):
                return target
    for key in ("data", "response", "result"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = next_url_from_payload(nested)
            if found:
                return found
    return None


def form_fields(form: Any) -> dict[str, str]:
    fields: dict[str, str] = {}
    if form is None:
        return fields
    for element in form.find_all(["input", "textarea", "select"]):
        name = element.get("name")
        if not name:
            continue
        input_type = (element.get("type") or "").lower()
        if input_type in {"submit", "button", "image", "reset", "file"}:
            continue
        if element.name == "select":
            option = element.find("option", selected=True) or element.find("option")
            value = ""
            if option is not None:
                value = option.get("value", option.get_text(strip=True))
        elif element.name == "textarea":
            value = element.get_text()
        else:
            value = element.get("value", "")
        fields[name] = value
    return fields


# ---------------------------------------------------------------------------
# JavaScript ofuscado
# ---------------------------------------------------------------------------


def js_unescape(text: str) -> str:
    if "\\" not in text:
        return text
    replacements = {
        "\\\\/": "/",
        "\\\\x2f": "/",
        "\\\\x3a": ":",
        "\\\\u002f": "/",
        "\\\\u003a": ":",
        "\\\\u0026": "&",
        "\\\\u003d": "=",
        "\\/": "/",
        "\\x2f": "/",
        "\\x3a": ":",
        "\\u002f": "/",
        "\\u003a": ":",
        "\\u0026": "&",
        "\\u003d": "=",
        "\\'": "'",
        '\\"': '"',
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def unpack_javascript(html: str) -> str | None:
    """Desempaqueta JS ofuscado con el packer de Dean Edwards."""
    match = re.search(
        r"eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e\s*,\s*[dr]\s*\)"
        r"\s*\{.*?\}\s*\(\s*'(.*?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.*?)'"
        r"\s*\.split\(\s*'\|'\s*\)",
        html,
        re.S,
    )
    if not match:
        return None
    payload, base, count, symbols = match.groups()
    try:
        base_number = int(base)
        total = int(count)
    except ValueError:
        return None
    words = symbols.split("|")

    def replace(match: re.Match[str]) -> str:
        word = match.group(0)
        try:
            index = int(word, base_number)
        except ValueError:
            return word
        if 0 <= index < total and index < len(words) and words[index]:
            return words[index]
        return word

    return re.sub(r"\w+", replace, payload)


def first_js_url(text: str, base_url: str) -> str | None:
    """Primera URL plausible dentro de un fragmento de JavaScript."""
    text = js_unescape(text)
    for match in _URL_IN_TEXT_RE.finditer(text):
        target = match.group(0).rstrip(").,;\"'")
        if target.rstrip("/") == base_url.rstrip("/"):
            continue
        if is_plausible_url(target):
            return target
    return None


def resolve_js_target(base_url: str, raw: str | None) -> str | None:
    """Resuelve un valor de JS: URL absoluta, relativa o base64."""
    raw = js_unescape((raw or "").strip())
    if not raw:
        return None
    decoded = decode_embedded_url(raw)
    if decoded:
        return decoded
    return absolute_url(base_url, raw)


def decode_adfly_ysmm(value: str) -> str | None:
    """Decodifica el token `ysmm` que usa adf.ly para esconder el destino."""
    value = (value or "").strip()
    if not value:
        return None
    left = ""
    right = ""
    for index, char in enumerate(value):
        if index % 2 == 0:
            left += char
        else:
            right = char + right
    encoded = left + right
    padded = encoded + "=" * (-len(encoded) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded = decoder(padded).decode("utf-8", "ignore")
        except Exception:
            continue
        for candidate in (decoded[16:-16], decoded):
            found = _URL_IN_TEXT_RE.search(candidate)
            if found:
                return found.group(0).rstrip(").,;\"'")
    return None


# Aliases privados para compatibilidad con código/documentación anterior.
_normalized_host = normalized_host
_is_plausible_url = is_plausible_url
_absolute_url = absolute_url
_form_action = form_action
_is_redirect_form = is_redirect_form
_js_unescape = js_unescape
_first_js_url = first_js_url
URL_IN_TEXT_RE = _URL_IN_TEXT_RE
