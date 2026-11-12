"""Catálogo de acortadores que usan plantillas genéricas.

No aportan un detector propio: sus páginas se resuelven con los plugins de
formularios, botones y JavaScript. Declararlos aquí mejora las heurísticas
(``looks_like_shortener``) y el tratamiento de ``/redirect?url=``.
"""

from __future__ import annotations

from plugins.base import Plugin

PLUGIN = Plugin(
    name="known-shorteners",
    priority=200,
    hosts=(
        # ouo
        "ouo.io",
        "ouo.press",
        "ouo.cash",
        # Clásicos con anuncios
        "shorte.st",
        "sh.st",
        "adfoc.us",
        "bc.vc",
        "festyy.com",
        "destyy.com",
        "fc.lc",
        "linkvertise.com",
        "linkvertise.net",
        "up-to-down.net",
        "sub2unlock.com",
        "sub2unlock.net",
        "sub2get.com",
        "boost.ink",
        "boostium.com",
        "mboost.me",
        "srt.am",
        "yoshort.com",
        "shortit.pw",
        "loot-link.com",
        "lootlinks.com",
        "loot-links.com",
        "socialwolvez.com",
        "gurubit.com",
        "miguelvz.com",
        "adshnk.com",
        "clickscoin.com",
    ),
    markers=(
        "get-link",
        "getlink",
        "continue-btn",
        "btn-continue",
        "skip-btn",
        "skipbutton",
        "btn-skip",
        "go-btn",
        "btn-main",
        "xreallcygo",
        "shrinkearn",
    ),
)
