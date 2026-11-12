"""Cliente opcional de Telethon para resolver enlaces de bots de Telegram.

Las credenciales se leen con :mod:`config` (llavero del sistema primero, luego
entorno y finalmente ``.env``). Nunca se imprimen sus valores.

Si faltan credenciales o la dependencia no está instalada se lanza
:class:`TelegramConfigError`, un ``ResolveError`` controlado con instrucciones.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import config
from desenlace_core import ResolveError, host_matches, normalized_host, sanitize_filename

TELEGRAM_HOSTS = ("t.me", "telegram.me", "telegram.dog", "tx.me")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
TEMP_PREFIX = "desenlace-telegram-"


class TelegramConfigError(ResolveError):
    """Faltan credenciales o dependencias de Telegram."""


@dataclass
class TelegramResult:
    final_url: str | None = None
    file_path: Path | None = None
    filename: str = ""
    message_url: str = ""
    message_text: str = ""


def telegram_configured() -> bool:
    return all(config.get_secret(name) for name in config.SECRET_NAMES)


def telegram_credentials() -> tuple[int, str, str]:
    """Devuelve (api_id, api_hash, session) o lanza TelegramConfigError."""
    missing = [name for name in config.SECRET_NAMES if not config.get_secret(name)]
    if missing:
        raise TelegramConfigError(
            "Telegram no está configurado. Faltan: "
            + ", ".join(missing)
            + ". Ejecuta `desenlace setup` para guardarlos en el "
            "llavero del sistema, o rellena el archivo .env (ver .env.example)."
        )
    api_id_text = config.get_secret("TELEGRAM_API_ID") or ""
    try:
        api_id = int(api_id_text)
    except ValueError as exc:
        raise TelegramConfigError("TELEGRAM_API_ID debe ser un número.") from exc
    return (
        api_id,
        config.get_secret("TELEGRAM_API_HASH") or "",
        config.get_secret("TELEGRAM_SESSION_STRING") or "",
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


async def _run_bot(bot: str, command: str, *, timeout: float, save: bool) -> TelegramResult:
    try:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise TelegramConfigError(
            "Telethon no está instalado. Ejecuta: pip install telethon"
        ) from exc

    api_id, api_hash, session_string = telegram_credentials()
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    received: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    async def on_new_message(event: Any) -> None:
        if not received.done():
            received.set_result(event.message)

    temp_dir: Path | None = None
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise TelegramConfigError(
                "La sesión de Telegram no está autorizada. Vuelve a generarla "
                "con `desenlace setup`."
            )
        try:
            entity = await client.get_entity(bot)
        except Exception as exc:
            raise TelegramConfigError(f"No se pudo abrir el bot @{bot}.") from exc

        client.add_event_handler(on_new_message, events.NewMessage(chats=entity))
        await client.send_message(entity, command)
        try:
            message = await asyncio.wait_for(received, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ResolveError(f"El bot @{bot} no respondió en {timeout:.0f}s.") from exc

        message_id = getattr(message, "id", "")
        message_url = f"https://t.me/{bot}/{message_id}".rstrip("/")
        message_text = getattr(message, "message", "") or ""
        media = getattr(message, "file", None)
        if media is not None:
            filename = sanitize_filename(
                getattr(media, "name", None) or f"telegram_{message_id or '0'}"
            )
            result = TelegramResult(
                filename=filename, message_url=message_url, message_text=message_text
            )
            if save:
                # Directorio propio por descarga: se borra entero si algo falla
                # o si el archivo termina en otro sitio.
                temp_dir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
                downloaded = await client.download_media(
                    message, file=str(temp_dir / filename)
                )
                if not downloaded:
                    raise ResolveError("Telegram no devolvió el contenido del archivo.")
                result.file_path = Path(downloaded)
            return result

        target = _extract_url_from_message(message)
        if target:
            return TelegramResult(
                final_url=target, message_url=message_url, message_text=message_text
            )
        raise ResolveError(
            f"El bot @{bot} respondió sin archivo ni enlace: {message_text[:200]!r}"
        )
    except BaseException:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def resolve_bot_link(url: str, *, timeout: float = 60.0, save: bool = True) -> TelegramResult:
    """Envía /start al bot y devuelve el archivo descargado o el enlace."""
    bot, command = parse_bot_link(url)
    if not telegram_configured():
        # Mismo mensaje controlado que en telegram_credentials().
        telegram_credentials()
    return asyncio.run(
        _run_bot(bot, command, timeout=max(5.0, timeout), save=save)
    )


def discard_temp_file(path: Path | None) -> None:
    """Elimina un archivo temporal de Telegram y su carpeta si la creamos nosotros."""
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
        if path.parent.name.startswith(TEMP_PREFIX):
            shutil.rmtree(path.parent, ignore_errors=True)
    except OSError:
        pass
