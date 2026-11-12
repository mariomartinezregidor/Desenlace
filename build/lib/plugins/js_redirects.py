"""Redirecciones por meta-refresh y JavaScript (incluido JS ofuscado)."""

from __future__ import annotations

import re

from desenlace_core import JS_REDIRECT_PATTERNS
from plugins.base import (
    Action,
    PageContext,
    Plugin,
    absolute_url,
    decode_embedded_url,
    first_js_url,
    is_plausible_url,
    resolve_js_target,
    unpack_javascript,
)


def detect_meta_refresh(ctx: PageContext) -> Action | None:
    meta = ctx.soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
    if meta is None:
        return None
    content = meta.get("content", "")
    match = re.search(r"url\s*=\s*([^;]+)", content, re.I)
    if not match:
        return None
    target = absolute_url(ctx.base_url, match.group(1).strip().strip("'\""))
    if target is None:
        return None
    return Action("goto", url=target)


def detect_js(ctx: PageContext) -> Action | None:
    html = ctx.html
    for pattern in JS_REDIRECT_PATTERNS:
        match = re.search(pattern, html)
        if not match:
            continue
        raw = match.group(1)
        target = resolve_js_target(ctx.base_url, raw)
        if (
            target
            and (
                raw.strip().startswith(("http://", "https://", "/", "./", "../"))
                or decode_embedded_url(raw)
            )
            and is_plausible_url(target)
        ):
            return Action("goto", url=target)

    for match in re.finditer(
        r'atob\(\s*["\']([A-Za-z0-9+/=_-]{8,})["\']\s*\)', html
    ):
        target = decode_embedded_url(match.group(1))
        if target and is_plausible_url(target):
            return Action("goto", url=target)

    if ctx.is_shortener:
        for match in re.finditer(
            r'unescape\(\s*["\']([^"\']{8,})["\']\s*\)', html
        ):
            target = decode_embedded_url(match.group(1))
            if target and is_plausible_url(target):
                return Action("goto", url=target)

        for match in re.finditer(
            r"String\.fromCharCode\(\s*([\d,\s]+)\s*\)", html
        ):
            try:
                decoded = "".join(
                    chr(int(code)) for code in re.findall(r"\d+", match.group(1))
                )
            except (ValueError, OverflowError):
                continue
            target = first_js_url(decoded, ctx.base_url)
            if target:
                return Action("goto", url=target)

        unpacked = unpack_javascript(html)
        if unpacked:
            target = first_js_url(unpacked, ctx.base_url)
            if target:
                return Action("goto", url=target)
    return None


PLUGINS = (
    Plugin(name="meta-refresh", priority=50, detect=detect_meta_refresh),
    Plugin(name="js-redirects", priority=60, detect=detect_js),
)
