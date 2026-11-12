"""Redirectores que llevan el destino en la propia URL.

Google, Bing, DuckDuckGo, Yahoo, Facebook, Reddit, VK, Tumblr, LinkedIn y
pasarelas genéricas ``/redirect?url=...``. El destino puede venir en claro,
URL-encoded o en base64.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from desenlace_core import (
    GATEWAY_PATHS,
    GENERIC_REDIRECT_PARAMS,
    REDIRECT_ENDPOINTS,
    Action,
    decode_embedded_url,
    host_matches,
    is_plausible_url,
    normalized_host,
)
from plugins.base import Plugin


def extract_redirect_target(
    url: str, shortener_hosts: tuple[str, ...] | None = None
) -> str | None:
    """Extrae el destino real de un redirector conocido."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = normalized_host(parts.netloc)
    path = (parts.path or "/").rstrip("/") or "/"
    query = parse_qs(parts.query)

    for domain, endpoint_path, params in REDIRECT_ENDPOINTS:
        if not host_matches(host, (domain,)):
            continue
        if endpoint_path is not None and path != endpoint_path.rstrip("/"):
            continue
        for name in params:
            for raw in query.get(name, []):
                target = decode_embedded_url(raw)
                if target and target != url:
                    return target

    if shortener_hosts is None:
        from plugins import shortener_hosts as all_shortener_hosts

        shortener_hosts = all_shortener_hosts()

    is_gateway = host_matches(host, shortener_hosts) or any(
        path == gateway or path.endswith(gateway) for gateway in GATEWAY_PATHS
    )
    if is_gateway:
        for name in GENERIC_REDIRECT_PARAMS:
            for raw in query.get(name, []):
                target = decode_embedded_url(raw)
                if target and target != url and is_plausible_url(target):
                    return target
    return None


def extract_search_redirect_target(url: str) -> str | None:
    """Compatibilidad con el nombre anterior."""
    return extract_redirect_target(url)


def detect_url(url: str, hosts: tuple[str, ...]) -> Action | None:
    target = extract_redirect_target(url, hosts)
    if target is None:
        return None
    return Action("goto", url=target)


PLUGIN = Plugin(
    name="redirectors",
    priority=5,
    detect_url=detect_url,
)
