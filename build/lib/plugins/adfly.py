"""adf.ly: destino escondido en el token JavaScript ``ysmm``."""

from __future__ import annotations

import re

from plugins.base import Action, PageContext, Plugin, decode_adfly_ysmm


def detect(ctx: PageContext) -> Action | None:
    match = re.search(r"""var\s+ysmm\s*=\s*['"]([^'"]+)['"]""", ctx.html)
    if not match:
        return None
    target = decode_adfly_ysmm(match.group(1))
    if target is None:
        return None
    return Action("goto", url=target)


PLUGIN = Plugin(
    name="adfly",
    priority=10,
    hosts=("adf.ly",),
    markers=("ysmm",),
    detect=detect,
)
