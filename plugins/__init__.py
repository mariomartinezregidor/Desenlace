"""Registro dinámico de plugins de Desenlace.

Para añadir un acortador nuevo basta con crear un archivo en esta carpeta que
exponga ``PLUGIN`` (o ``PLUGINS``) con una instancia de :class:`Plugin`.

Ejemplo mínimo::

    from plugins.base import Action, PageContext, Plugin

    def detect(ctx: PageContext) -> Action | None:
        boton = ctx.soup.find("a", id="mi-boton")
        if boton is None:
            return None
        return Action("goto", url=boton["href"])

    PLUGIN = Plugin(
        name="mi-sitio",
        priority=50,
        hosts=("mi-sitio.example",),
        markers=("mi-boton",),
        detect=detect,
    )
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
import warnings
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from desenlace_core import Action, PageContext, host_matches
from plugins.base import Plugin

__all__ = [
    "PLUGINS",
    "SHORTENER_HOSTS",
    "SHORTENER_MARKERS",
    "build_browser_plan",
    "detect_action",
    "discover_plugins",
    "looks_like_shortener",
    "normalize_url",
    "plugin_names",
    "shortener_hosts",
    "shortener_markers",
]

_PLUGINS: tuple[Plugin, ...] | None = None
_AGGREGATES: tuple[tuple[str, ...], tuple[str, ...]] | None = None
_LOCK = threading.Lock()


def discover_plugins(*, force: bool = False) -> tuple[Plugin, ...]:
    """Importa y ordena todos los plugins de la carpeta."""
    global _PLUGINS, _AGGREGATES
    with _LOCK:
        if _PLUGINS is not None and not force:
            return _PLUGINS

        directory = Path(__file__).resolve().parent
        found: list[Plugin] = []
        for info in pkgutil.iter_modules([str(directory)]):
            if info.name == "base" or info.name.startswith("_"):
                continue
            try:
                module = importlib.import_module(f"{__name__}.{info.name}")
            except Exception as exc:  # plugin roto: no debe tumbar el motor
                warnings.warn(f"No se pudo cargar el plugin {info.name!r}: {exc}")
                continue

            candidates = getattr(module, "PLUGINS", None)
            if candidates is None:
                single = getattr(module, "PLUGIN", None)
                candidates = [single] if single is not None else []
            for candidate in candidates:
                if not isinstance(candidate, Plugin):
                    warnings.warn(
                        f"El plugin {info.name!r} no expone una instancia Plugin válida"
                    )
                    continue
                found.append(candidate)

        by_name: dict[str, Plugin] = {}
        for plugin in found:
            if plugin.name in by_name:
                warnings.warn(f"Plugin duplicado {plugin.name!r}; se usa el último")
            by_name[plugin.name] = plugin

        _PLUGINS = tuple(
            sorted(by_name.values(), key=lambda item: (item.priority, item.name))
        )
        _AGGREGATES = None
        return _PLUGINS


def _aggregate() -> tuple[tuple[str, ...], tuple[str, ...]]:
    global _AGGREGATES
    if _AGGREGATES is not None:
        return _AGGREGATES
    hosts: set[str] = set()
    markers: set[str] = set()
    for plugin in discover_plugins():
        hosts.update(plugin.hosts)
        markers.update(marker.lower() for marker in plugin.markers)
    _AGGREGATES = (tuple(sorted(hosts)), tuple(sorted(markers)))
    return _AGGREGATES


def shortener_hosts() -> tuple[str, ...]:
    return _aggregate()[0]


def shortener_markers() -> tuple[str, ...]:
    return _aggregate()[1]


def looks_like_shortener(html: str, url: str) -> bool:
    """Heurística para distinguir una página de espera del destino final."""
    try:
        parts = urlsplit(url)
        host = parts.netloc or parts.path
    except ValueError:
        host = ""
    if host_matches(host, shortener_hosts()):
        return True
    low_html = (html or "").lower()
    return any(marker in low_html for marker in shortener_markers())


def detect_action(html: str, base_url: str) -> Action | None:
    """Ejecuta los plugins en orden de prioridad y devuelve la primera acción."""
    # Las páginas de WordPress (artículos de relleno de redes de anuncios) no
    # son acortadores reales: sus formularios y enlaces "siguiente" son falsos
    # positivos que rompen el flujo. Se dejan para el bypass con navegador.
    low = (html or "").lower()
    # `wp-content/uploads` también aparece en imágenes de anuncios dentro de
    # acortadores, así que exigimos marcadores propios de una página WordPress.
    if (
        "wp-comments-post" in low
        or "wp-json" in low
        or "wp-content/themes" in low
    ):
        return None
    plugins = discover_plugins()
    hosts = shortener_hosts()

    # 1) Redirectores cuyo destino viaja en la URL (no requieren HTML).
    for plugin in plugins:
        if plugin.detect_url is None:
            continue
        action = plugin.detect_url(base_url, hosts)
        if action is not None:
            return action

    # 2) Detectores sobre el DOM.
    soup = BeautifulSoup(html, "html.parser")
    ctx = PageContext(
        html=html,
        base_url=base_url,
        soup=soup,
        is_shortener=looks_like_shortener(html, base_url),
    )
    for plugin in plugins:
        if plugin.detect is None:
            continue
        action = plugin.detect(ctx)
        if action is not None:
            return action
    return None


def build_browser_plan(html: str, base_url: str) -> list[dict[str, object]] | None:
    """Pide a los plugins un plan de acciones para el navegador headless.

    Se consulta cuando el motor HTTP no encuentra una acción válida. El primer
    plugin que devuelve un plan no vacío gana.
    """
    soup = BeautifulSoup(html, "html.parser")
    ctx = PageContext(
        html=html,
        base_url=base_url,
        soup=soup,
        is_shortener=looks_like_shortener(html, base_url),
    )
    for plugin in discover_plugins():
        if plugin.browser_plan is None:
            continue
        try:
            plan = plugin.browser_plan(ctx)
        except Exception as exc:  # un plan roto no debe tumbar la resolución
            warnings.warn(f"El plugin {plugin.name!r} falló al planificar: {exc}")
            continue
        if plan:
            return list(plan)
    return None


def normalize_url(url: str) -> str:
    """Aplica los plugins de normalización (hostings -> enlace directo)."""
    current = url
    for _ in range(3):
        changed = False
        for plugin in discover_plugins():
            if plugin.normalize is None:
                continue
            try:
                candidate = plugin.normalize(current)
            except Exception:
                continue
            if candidate and candidate != current:
                current = candidate
                changed = True
        if not changed:
            break
    return current


def plugin_names() -> tuple[str, ...]:
    return tuple(plugin.name for plugin in discover_plugins())


PLUGINS = discover_plugins()
SHORTENER_HOSTS = shortener_hosts()
SHORTENER_MARKERS = shortener_markers()
