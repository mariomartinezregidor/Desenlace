"""wp-safelink y safelinks de Blogger/WordPress con payload base64."""

from __future__ import annotations

from urllib.parse import parse_qs, urljoin, urlsplit

from desenlace_core import ResolveError
from plugins.base import (
    Action,
    PageContext,
    Plugin,
    decode_safelink,
    form_fields,
    safelink_delay,
    safelink_target,
)


def detect(ctx: PageContext) -> Action | None:
    soup = ctx.soup
    safelink_input = soup.find("input", {"name": "newwpsafelink"})
    raw_value = safelink_input.get("value") if safelink_input is not None else None

    if not raw_value:
        query = parse_qs(urlsplit(ctx.base_url).query)
        for name in ("newwpsafelink", "safelink", "go", "link"):
            for candidate in query.get(name, []):
                try:
                    decode_safelink(candidate)
                except ResolveError:
                    continue
                raw_value = candidate
                break
            if raw_value:
                break

    if not raw_value:
        return None

    payload = decode_safelink(raw_value)
    delay = safelink_delay(payload)
    target = safelink_target(payload)
    if target:
        return Action("wait_get", url=urljoin(ctx.base_url, target), delay=delay)

    if safelink_input is not None:
        form = safelink_input.find_parent("form")
        if form is not None:
            return Action(
                "wait_post_form",
                url=urljoin(ctx.base_url, form.get("action") or ctx.base_url),
                fields=form_fields(form),
                delay=delay,
            )
    return None


PLUGIN = Plugin(
    name="wp-safelink",
    priority=25,
    hosts=(
        "safelinku.net",
        "safelinku.com",
        "safelinkconverter.com",
        "sfl.gl",
        "tii.ai",
        "tei.ai",
    ),
    markers=("newwpsafelink", "safelinku", "bloggerpemula"),
    detect=detect,
)
