"""Multi-Click Download Bypasser.

Detecta páginas de descarga con varios pasos ("Paso 1: clic aquí", espera de
10 s, cerrar pop-up, "Descargar") y genera un plan para el navegador headless.
El motor sólo ejecuta este plan cuando el flujo HTTP no puede avanzar, así que
no interfiere con los plugins de resolución normales.
"""

from __future__ import annotations

import re

from plugins.base import PageContext, Plugin

CLICK_KEYWORDS = (
    "download",
    "descargar",
    "descarga",
    "getlink",
    "get link",
    "continue",
    "continuar",
    "skip",
    "saltar",
    "next",
    "siguiente",
    "click",
    "clic",
    "proceed",
    "verify",
    "verificar",
    "unlock",
    "generate",
    "generar",
    "obtener",
    "step",
    "paso",
)

TIMER_RE = re.compile(
    r"(counter_value|custom-timer|countdown|data-timer|id=['\"]timer|"
    r"please wait|esperando|waiting for|seconds remaining|segundos restantes|"
    r"espera \d+)",
    re.I,
)

# Selectores genéricos que suelen contener el enlace/botón final.
DOWNLOAD_SELECTORS = (
    "a[download]",
    "a#downloadButton",
    "a#download-button",
    "a#download_link",
    "a[href*='download']",
    "form[action*='download'] button",
    "form[action*='download'] input[type='submit']",
)


def _selector_for(element: object) -> str | None:
    element_id = (element.get("id") or "").strip()
    if element_id:
        return f"#{element_id}"
    classes = [cls for cls in element.get("class", []) if cls]
    if classes:
        return f".{classes[0]}"
    return None


def _element_label(element: object) -> str:
    return " ".join(
        [
            (element.get("id") or ""),
            " ".join(element.get("class", [])),
            (element.get("value") or ""),
            element.get_text(" ", strip=True),
        ]
    ).lower()


def detect_browser_plan(ctx: PageContext) -> list[dict[str, object]] | None:
    soup = ctx.soup
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()

    elements = list(soup.find_all(["button", "a", "input"]))
    elements.extend(soup.select("[role=button]"))
    for element in elements:
        tag = getattr(element, "name", "")
        element_type = (element.get("type") or "").lower()
        if tag == "input" and element_type not in ("submit", "button"):
            continue
        label = _element_label(element)
        if not any(keyword in label for keyword in CLICK_KEYWORDS):
            continue
        # "Skip to content" no avanza nada: es un ancla interna.
        if "skip to" in label or "skip-link" in label:
            continue
        selector = _selector_for(element)
        step: dict[str, object] = {"kind": "click"}
        if selector:
            step["selector"] = selector
            key = selector
        else:
            text = element.get_text(" ", strip=True)[:80]
            if not text:
                continue
            step["text"] = text
            key = f"text:{text}"
        if key in seen:
            continue
        seen.add(key)
        candidates.append(step)
        if len(candidates) >= 6:
            break

    for form in soup.find_all("form", action=True):
        action = (form.get("action") or "").lower()
        # El formulario de comentarios de WordPress no lleva al archivo.
        if "wp-comments-post" in action or "wp-" in action:
            continue
        selector = "form[action] button[type='submit'], form[action] input[type='submit']"
        if selector in seen:
            continue
        seen.add(selector)
        candidates.append({"kind": "click", "selector": selector})

    # Prioriza el botón real de descarga frente a badges/tutoriales.
    def _prioridad(step: dict[str, object]) -> int:
        texto = f"{step.get('selector', '')} {step.get('text', '')}".lower()
        if "download" in texto or "descargar" in texto or "mainbutton" in texto:
            return 0
        return 1

    candidates.sort(key=_prioridad)

    has_timer = bool(TIMER_RE.search(ctx.html))
    if not candidates and not has_timer:
        return None
    # En hosts desconocidos exigimos un temporizador explícito para no lanzar
    # el navegador en artículos con botones "siguiente".
    if not ctx.is_shortener and not has_timer:
        return None

    plan: list[dict[str, object]] = [
        {"kind": "wait", "seconds": 8.0},
        # Intersticiales de publicidad (cloudpiko y similares): hay que cerrar
        # el overlay antes de poder pulsar el botón real de descarga.
        {"kind": "click", "selector": "[class*='__closelink']"},
        {"kind": "close_popup"},
        {"kind": "click", "selector": "[class*='__close']"},
        {"kind": "wait", "seconds": 1.0},
    ]
    for step in candidates:
        plan.append(step)
        # Si el botón no navega por sí solo (web-api-*.vercel.app), lanzar su API.
        if _prioridad(step) == 0:
            plan.append({"kind": "shorten"})
        plan.append({"kind": "close_popup"})
        plan.append({"kind": "wait", "seconds": 1.0})
    # Dar tiempo a que la petición complete su navegación.
    plan.append({"kind": "wait", "seconds": 8.0})
    plan.append({"kind": "extract_url"})
    for selector in DOWNLOAD_SELECTORS:
        plan.append({"kind": "extract_url", "selector": selector})
    return plan


PLUGIN = Plugin(
    name="multi-click-bypasser",
    priority=150,
    markers=(
        "paso 1",
        "step 1",
        "haz clic",
        "click here",
        "click here to continue",
        "close popup",
    ),
    browser_plan=detect_browser_plan,
)
