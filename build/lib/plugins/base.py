"""Contrato de los plugins de Desenlace.

Un plugin es un módulo dentro de ``plugins/`` que expone ``PLUGIN`` (o
``PLUGINS``). Sólo necesita declarar sus hosts/marcadores y una función de
detección. El núcleo descubre e importa los módulos automáticamente.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

# Re-exportamos las primitivas que usan los plugins para que cada módulo sólo
# necesite una línea de imports.
from desenlace_core import (  # noqa: F401
    Action,
    PageContext,
    absolute_url,
    decode_adfly_ysmm,
    decode_embedded_url,
    decode_safelink,
    filename_from_url,
    first_js_url,
    form_action,
    form_fields,
    host_matches,
    is_plausible_url,
    is_redirect_form,
    js_unescape,
    parse_counter,
    resolve_js_target,
    safelink_delay,
    safelink_target,
    unpack_javascript,
)

Detect = Callable[[PageContext], "Action | None"]
DetectUrl = Callable[[str, "tuple[str, ...]"], "Action | None"]
Normalize = Callable[[str], str]
BrowserPlan = Callable[[PageContext], "list[dict[str, Any]] | None"]


@dataclass(frozen=True)
class Plugin:
    """Estrategia de resolución para una familia de acortadores.

    Attributes:
        name: identificador único (normalmente el nombre del archivo).
        priority: menor número = se ejecuta antes.
        hosts: dominios que cubre (se usan también para las heurísticas).
        markers: cadenas que delatan una página de espera.
        detect: función ``(PageContext) -> Action | None`` sobre el HTML.
        detect_url: función ``(url, shortener_hosts) -> Action | None`` que
            resuelve redirecciones que viajan en la propia URL.
        normalize: función ``url -> url`` para convertir páginas de hosting
            en enlaces directos antes de hacer peticiones.
        browser_plan: función ``(PageContext) -> list[dict] | None`` que
            describe una secuencia de acciones para el navegador headless
            (``CLICK``, ``WAIT``, ``CLOSE_POPUP``, ``EXTRACT_URL``). El motor
            la usa sólo cuando el flujo HTTP no puede avanzar.
    """

    name: str
    priority: int = 100
    hosts: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    detect: Detect | None = None
    detect_url: DetectUrl | None = None
    normalize: Normalize | None = None
    browser_plan: BrowserPlan | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("El plugin necesita un nombre")


def as_tuple(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(v for v in (values or ()) if v)
