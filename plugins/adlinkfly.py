"""Familia AdLinkFly y clones (gplinks, shrinkme, try2link, droplink...)."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from plugins.base import Action, PageContext, Plugin, form_fields


def detect(ctx: PageContext) -> Action | None:
    soup = ctx.soup
    form = soup.find("form", id="go-link")
    if form is None:
        ad_input = soup.find("input", {"name": "ad_form_data"})
        if ad_input is not None:
            form = ad_input.find_parent("form")
    if form is None:
        form = soup.find("form", attrs={"action": re.compile(r"/links/go", re.I)})
    if form is None:
        return None
    fields = form_fields(form)
    if not fields:
        return None
    action_url = urljoin(ctx.base_url, form.get("action") or ctx.base_url)
    return Action("xhr_post", url=action_url, fields=fields, delay=ctx.counter)


PLUGIN = Plugin(
    name="adlinkfly",
    priority=20,
    hosts=(
        "adlinkfly.com",
        "gplinks.co",
        "gplinks.in",
        "try2link.com",
        "droplink.co",
        "mdiskshortner.link",
        "tnlink.in",
        "urlsopen.com",
        "ez4short.com",
        "vearnbux.com",
        "earn4link.in",
        "rocklink.in",
        "clk.ink",
        "shortxlinks.in",
        "shortxlinks.com",
        "softurl.in",
        "zflexlink.com",
        "zflexlinks.com",
        "iflylink.com",
        "shrinkme.io",
        "shrinke.me",
        "shrinkearn.com",
        "exe.io",
        "exey.io",
        "za.gl",
        "zagl.xyz",
        "zshort.link",
        "shortzon.com",
        "clicksfly.com",
        "megaurl.in",
        "linkshrink.net",
        "cuty.io",
        "shrinkforearn.in",
    ),
    markers=("ad_form_data", "go-link", "links/go", "counter_value"),
    detect=detect,
)
