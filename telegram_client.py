"""Cliente opcional de Telethon para resolver enlaces de bots de Telegram.

Configuración por entorno (nunca se registran los valores):

    TELEGRAM_API_ID=123456
    TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
    TELEGRAM_SESSION_STRING=...

Sin esas variables se lanza :class:`TelegramConfigError`, un ``ResolveError``
controlado que el motor muestra al usuario con instrucciones.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from desenlace_core import ResolveError, host_matches, normalized_host, sanitize_filename

TELEGRAM_HOSTS = ("t.me", "telegram.me", "telegram.dog", "tx.me")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")


class TelegramConfigError(ResolveError):
    """Faltan credenciales o dependencias de Telegram."""


@dataclass
class TelegramResult:
    final_url: str | None = None
    file_path: Path | None = None
    filename: str = ""
    message_url: str = ""
    message_text: str = ""


def _load_dotenv(path: str | Path | None = None) -> None:
    """Carga un .env sencillo sin dependencias externas (no sobreescribe)."""
    env_path = Path(path or os.environ.get("DESENLACE_ENV") or Path(__file__).parent / ".env")
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def telegram_configured() -> bool:
    _load_dotenv()
    return bool(
        os.environ.get("TELEGRAM_API_ID")
        and os.environ.get("TELEGRAM_API_HASH")
        and os.environ.get("TELEGRAM_SESSION_STRING")
    )


def parse_bot_link(url: str) -> tuple[str, str]:
    """Devuelve (usuario_bot, comando) a partir de un enlace t.me."""
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise TelegramConfigError(f"Enlace de Telegram inválido: {url}") from exc
    host = normalized_host(parts.netloc)
    if not host_matches(host, TELEGRAM_HOSTS):
        raise TelegramConfigError(f"No es un enlace de Telegram: {url}")
    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments:
        raise TelegramConfigError(f"El enlace no indica bot: {url}")
    bot = segments[0].lstrip("@")
    query = parse_qs(parts.query)
    start = (query.get("start") or [""])[0]
    command = f"/start {start}".strip() if start else "/start"
    return bot, command


def _extract_url_from_message(message: Any) -> str | None:
    text = getattr(message, "message", "") or ""
    match = URL_RE.search(text)
    if match:
        return match.group(0).rstrip(").,;\"'")
    markup = getattr(message, "reply_markup", None)
    if markup is not None:
        for row in getattr(markup, "rows", []) or []:
            for button in getattr(row, "buttons", []) or []:
                button_url = getattr(button, "url", None)
                if button_url:
                    return str(button_url)
    return None


async def _run_bot(
    bot: str,
    command: str,
    *,
    timeout: float,
    save: bool,
    output_dir: str | None,
) -> TelegramResult:
    try:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise TelegramConfigError(
            "Telethon no está instalado. Ejecuta: pip install telethon"
        ) from exc

    api_id_text = os.environ.get("TELEGRAM_API_ID", "")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    session_string = os.environ.get("TELEGRAM_SESSION_STRING", "")
    if not (api_id_text and api_hash and session_string):
        raise TelegramConfigError(
            "Telegram no está configurado. Define TELEGRAM_API_ID, "
            "TELEGRAM_API_HASH y TELEGRAM_SESSION_STRING (ver .env.example)."
        )
    try:
        api_id = int(api_id_text)
    except ValueError as exc:
        raise TelegramConfigError("TELEGRAM_API_ID debe ser un número.") from exc

    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    received: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    async def on_new_message(event: Any) -> None:
        if not received.done():
            received.set_result(event.message)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise TelegramConfigError(
                "La TELEGRAM_SESSION_STRING no está autorizada. Genera una nueva "
                "sesión con Telethon."
            )
        try:
            entity = await client.get_entity(bot)
        except Exception as exc:
            raise TelegramConfigError(
                f"No se pudo abrir el bot @{bot}: {exc}"
            ) from exc

        client.add_event_handler(on_new_message, events.NewMessage(chats=entity))
        await client.send_message(entity, command)
        try:
            message = await asyncio.wait_for(received, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ResolveError(
                f"El bot @{bot} no respondió en {timeout:.0f}s."
            ) from exc

        message_url = f"https://t.me/{bot}/{getattr(message, 'id', '')}".rstrip("/")
        media = getattr(message, "file", None)
        if media is not None:
            filename = sanitize_filename(
                getattr(media, "name", None) or f"telegram_{getattr(message, 'id', '0')}"
            )
            result = TelegramResult(
                filename=filename,
                message_url=message_url,
                message_text=getattr(message, "message", "") or "",
            )
            if save:
                directory = Path(
                    output_dir
                    or tempfile.mkdtemp(prefix="desenlace-telegram-")
                ).expanduser()
                directory.mkdir(parents=True, exist_ok=True)
                downloaded = await client.download_media(
                    message, file=str(directory / filename)
                )
                if downloaded:
                    result.file_path = Path(downloaded)
            return result

        target = _extract_url_from_message(message)
        if target:
            return TelegramResult(
                final_url=target,
                message_url=message_url,
                message_text=getattr(message, "message", "") or "",
            )
        raise ResolveError(
            f"El bot @{bot} respondió sin archivo ni enlace: "
            f"{(getattr(message, 'message', '') or '')[:200]!r}"
        )
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def resolve_bot_link(
    url: str,
    *,
    timeout: float = 60.0,
    save: bool = True,
    output_dir: str | None = None,
) -> TelegramResult:
    """Envía /start al bot y devuelve el archivo descargado o el enlace."""
    if not telegram_configured():
        raise TelegramConfigError(
            "Telegram no está configurado. Define TELEGRAM_API_ID, "
            "TELEGRAM_API_HASH y TELEGRAM_SESSION_STRING en el entorno "
            "(ver .env.example)."
        )
    bot, command = parse_bot_link(url)
    return asyncio.run(
        _run_bot(
            bot,
            command,
            timeout=max(5.0, timeout),
            save=save,
            output_dir=output_dir,
        )
    )
