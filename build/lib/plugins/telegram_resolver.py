"""Resolver de enlaces de Telegram (canales públicos y bots).

- Canal público (`t.me/canal/123`): se abre el preview `t.me/s/canal[/123]`
  con Playwright y se extrae el documento/vídeo del DOM.
- Bot (`t.me/bot?start=...`): se deriva a `telegram_client.py` (Telethon), que
  requiere credenciales en el entorno. Sin credenciales devuelve un error
  controlado con instrucciones.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from desenlace_core import Action, host_matches, normalized_host
from plugins.base import Plugin

TELEGRAM_HOSTS = ("t.me", "telegram.me", "telegram.dog", "tx.me")

# Selectores de documentos, vídeos, audio y fotos en el preview web público.
TELEGRAM_DOWNLOAD_SELECTOR = ", ".join(
    (
        ".tgme_widget_message_document_wrap a[href]",
        ".tgme_widget_message_document a[href]",
        ".tgme_widget_message_document_title",
        "a[href*='cdn-telegram.org']",
        "a[href*='telegram-cdn.org']",
        "a[href*='telesco.pe']",
        ".tgme_widget_message_video_player video[src]",
        ".tgme_widget_message_voice audio[src]",
        ".tgme_widget_message_photo_wrap a[href]",
    )
)


def parse_telegram_link(url: str) -> dict[str, str] | None:
    """Clasifica un enlace de Telegram en canal o bot."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = normalized_host(parts.netloc)
    if not host_matches(host, TELEGRAM_HOSTS):
        return None

    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments:
        return None
    if segments[0].lower() == "s":
        segments = segments[1:]
    if not segments:
        return None

    name = segments[0]
    query = parse_qs(parts.query)
    if name.lower().endswith("bot"):
        start = (query.get("start") or [""])[0]
        return {"kind": "bot", "bot": name, "start": start}

    message_id = segments[1] if len(segments) > 1 and segments[1].isdigit() else ""
    return {"kind": "channel", "channel": name, "message_id": message_id}


def channel_preview_url(channel: str, message_id: str = "") -> str:
    if message_id:
        return f"https://t.me/s/{channel}/{message_id}"
    return f"https://t.me/s/{channel}"


def channel_plan() -> list[dict[str, object]]:
    """Plan de acciones para extraer el archivo del preview del canal."""
    return [
        {"kind": "wait", "seconds": 1.5},
        {"kind": "extract_url", "selector": TELEGRAM_DOWNLOAD_SELECTOR},
    ]


def detect_url(url: str, hosts: tuple[str, ...]) -> Action | None:
    info = parse_telegram_link(url)
    if info is None:
        return None
    if info["kind"] == "bot":
        return Action("telegram_bot", url=url, filename=info.get("start", ""))
    preview = channel_preview_url(info["channel"], info.get("message_id", ""))
    return Action("browser", url=preview, plan=channel_plan())


PLUGIN = Plugin(
    name="telegram-resolver",
    priority=15,
    hosts=TELEGRAM_HOSTS,
    detect_url=detect_url,
)
