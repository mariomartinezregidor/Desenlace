"""Heurísticas sobre enlaces, botones, atributos data-* e iframes."""

from __future__ import annotations

import re

from desenlace_core import JS_REDIRECT_PATTERNS
from plugins.base import (
    Action,
    PageContext,
    Plugin,
    absolute_url,
    decode_embedded_url,
    filename_from_url,
    is_plausible_url,
    resolve_js_target,
)

BUTTON_SELECTORS = (
    "#btn-main",
    "#main-button",
    "#continue",
    "#getlink",
    "#get-link",
    "#btn-continue",
    "#btn-skip",
    "#submit-button",
    "a#invisibleCaptchaShortlink",
)

DATA_ATTRIBUTES = (
    "data-href",
    "data-url",
    "data-link",
    "data-target",
    "data-redirect",
    "data-destination",
    "data-clipboard-text",
)

CONTINUE_KEYWORDS = (
    "continue",
    "skip",
    "get link",
    "getlink",
    "download",
    "continuar",
    "saltar",
    "proceed",
    "next",
    "siguiente",
    "obtener enlace",
    "generar enlace",
    "ir al enlace",
    "abrir enlace",
    "acceder",
    "descargar",
    "unlock",
    "verify",
    "go to link",
    "click here",
)


def detect_download_attributes(ctx: PageContext) -> Action | None:
    soup = ctx.soup
    candidates = list(soup.find_all("a", attrs={"download": True}, href=True))
    if ctx.is_shortener:
        for anchor in soup.find_all("a", href=True):
            classes = " ".join(anchor.get("class", []))
            if re.search(r"download", classes, re.I):
                candidates.append(anchor)
    for anchor in candidates:
        target = absolute_url(ctx.base_url, anchor.get("href"))
        if target is None or target.rstrip("/") == ctx.base_url.rstrip("/"):
            continue
        return Action(
            "mediafire", url=target, filename=filename_from_url(target) or ""
        )
    return None


def detect_data_attributes(ctx: PageContext) -> Action | None:
    if not ctx.is_shortener:
        return None
    for attribute in DATA_ATTRIBUTES:
        for element in ctx.soup.find_all(attrs={attribute: True}):
            raw = element.get(attribute)
            target = decode_embedded_url(raw) or absolute_url(ctx.base_url, raw)
            if target is None or target.rstrip("/") == ctx.base_url.rstrip("/"):
                continue
            if is_plausible_url(target):
                return Action("goto", url=target)
    return None


def detect_known_buttons(ctx: PageContext) -> Action | None:
    if not ctx.is_shortener:
        return None
    for selector in BUTTON_SELECTORS:
        node = ctx.soup.select_one(selector)
        if node is None:
            continue
        target = absolute_url(
            ctx.base_url, node.get("href") or node.get("formaction")
        )
        if target and is_plausible_url(target):
            return Action("goto", url=target)
    return None


def detect_iframe(ctx: PageContext) -> Action | None:
    if not ctx.is_shortener:
        return None
    for frame in ctx.soup.find_all(["iframe", "frame"], src=True):
        target = absolute_url(ctx.base_url, frame.get("src"))
        if target and is_plausible_url(target):
            return Action("goto", url=target)
    return None


def detect_continue_links(ctx: PageContext) -> Action | None:
    if not ctx.is_shortener:
        return None
    for anchor in ctx.soup.find_all("a", href=True):
        label = " ".join(
            [
                anchor.get_text(" ", strip=True).lower(),
                " ".join(anchor.get("class", [])).lower(),
                (anchor.get("id") or "").lower(),
            ]
        )
        if not any(keyword in label for keyword in CONTINUE_KEYWORDS):
            continue
        target = absolute_url(ctx.base_url, anchor["href"])
        if target is None or target.rstrip("/") == ctx.base_url.rstrip("/"):
            continue
        if is_plausible_url(target):
            return Action("goto", url=target)
    return None


def detect_onclick(ctx: PageContext) -> Action | None:
    if not ctx.is_shortener:
        return None
    patterns = JS_REDIRECT_PATTERNS + (r'window\.open\(\s*["\']([^"\']+)["\']',)
    for element in ctx.soup.find_all(attrs={"onclick": True}):
        onclick = element.get("onclick", "")
        for pattern in patterns:
            match = re.search(pattern, onclick)
            if not match:
                continue
            target = resolve_js_target(ctx.base_url, match.group(1))
            if target and is_plausible_url(target):
                return Action("goto", url=target)
    return None


def detect_js_variables(ctx: PageContext) -> Action | None:
    if not ctx.is_shortener:
        return None
    patterns = (
        r'(?:var|let|const)\s+\w*(?:url|link|redirect|target|dest)\w*\s*=\s*["\']([^"\']+)["\']',
        r'setTimeout\(\s*["\'][^"\']*?location(?:\.href)?\s*=\s*["\']([^"\']+)["\'][^"\']*?["\']',
        r'setTimeout\(\s*function\s*\(\)\s*\{[^}]*?location(?:\.href)?\s*=\s*["\']([^"\']+)["\'][^}]*?\}\s*,',
        r'setTimeout\(\s*\(\)\s*=>\s*\{?[^}]*?location(?:\.href)?\s*=\s*["\']([^"\']+)["\']',
        r'window\.open\(\s*["\']([^"\']+)["\']',
        r'location\s*=\s*["\']([^"\']+)["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, ctx.html, re.I | re.S)
        if not match:
            continue
        target = resolve_js_target(ctx.base_url, match.group(1))
        if target and is_plausible_url(target):
            return Action("goto", url=target)
    return None


PLUGINS = (
    Plugin(name="download-attributes", priority=70, detect=detect_download_attributes),
    Plugin(name="data-attributes", priority=75, detect=detect_data_attributes),
    Plugin(name="known-buttons", priority=78, detect=detect_known_buttons),
    Plugin(name="iframe-redirect", priority=80, detect=detect_iframe),
    Plugin(name="continue-links", priority=90, detect=detect_continue_links),
    Plugin(name="onclick-redirect", priority=95, detect=detect_onclick),
    Plugin(name="js-variables", priority=100, detect=detect_js_variables),
)
