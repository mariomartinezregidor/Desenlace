#!/usr/bin/env python3
"""Desenlace: resuelve enlaces con anuncios y descarga el archivo final.

Uso:
    python3 desenlace.py "https://enlace"
    python3 desenlace.py setup

Este módulo es sólo un punto de entrada y una fachada de compatibilidad: el
trabajo real está repartido en cli.py, orchestrator.py, resolver.py,
downloader.py y desenlace_core.py. La interfaz web usa desde aquí los mismos
nombres de siempre.
"""

from __future__ import annotations

from cli import (
    build_parser,
    collect_urls,
    dedupe,
    main,
    parse_extra_headers,
    read_urls_file,
)
from browser_resolver import BrowserResolver, is_bot_challenge
from desenlace_core import (
    CACHE_FILE,
    DEFAULT_CACHE_TTL,
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_RETRIES,
    Action,
    PageContext,
    ResolveError,
    UnsafeUrlError,
    base_destination,
    decode_adfly_ysmm,
    decode_embedded_url,
    ensure_extension,
    extract_wrapper_target,
    filename_from_disposition,
    filename_from_url,
    host_matches,
    next_url_from_payload,
    parse_counter,
    redact_url,
    sanitize_filename,
    unpack_javascript,
    validate_url,
)
from models import JobResult, Resolution
from orchestrator import Hooks, ProcessOptions, process_url
from plugins import (
    PLUGINS,
    SHORTENER_HOSTS,
    SHORTENER_MARKERS,
    build_browser_plan,
    detect_action,
    looks_like_shortener,
    normalize_url,
)
from plugins.file_hosts import normalize_file_host_url
from plugins.redirectors import extract_redirect_target, extract_search_redirect_target
from resolver import (
    LinkResolver,
    ResolutionCache,
    browser_resolve_and_download,
    cache_key,
    is_html_response,
    load_cookie_file,
)
from safe_http import SafeSession

# Nombres históricos que siguen disponibles para scripts y tests.
is_challenge = is_bot_challenge
redact = redact_url

__all__ = [
    "Action",
    "BrowserResolver",
    "CACHE_FILE",
    "DEFAULT_CACHE_TTL",
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_RETRIES",
    "Hooks",
    "JobResult",
    "LinkResolver",
    "PLUGINS",
    "PageContext",
    "ProcessOptions",
    "Resolution",
    "ResolutionCache",
    "ResolveError",
    "SHORTENER_HOSTS",
    "SHORTENER_MARKERS",
    "SafeSession",
    "UnsafeUrlError",
    "base_destination",
    "browser_resolve_and_download",
    "build_browser_plan",
    "build_parser",
    "cache_key",
    "collect_urls",
    "decode_adfly_ysmm",
    "decode_embedded_url",
    "dedupe",
    "detect_action",
    "ensure_extension",
    "extract_redirect_target",
    "extract_search_redirect_target",
    "extract_wrapper_target",
    "filename_from_disposition",
    "filename_from_url",
    "host_matches",
    "is_bot_challenge",
    "is_challenge",
    "is_html_response",
    "load_cookie_file",
    "looks_like_shortener",
    "main",
    "normalize_file_host_url",
    "next_url_from_payload",
    "normalize_url",
    "parse_counter",
    "parse_extra_headers",
    "process_url",
    "read_urls_file",
    "redact",
    "redact_url",
    "sanitize_filename",
    "unpack_javascript",
    "validate_url",
]

if __name__ == "__main__":
    raise SystemExit(main())
