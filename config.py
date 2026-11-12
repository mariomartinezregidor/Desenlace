"""Configuración y secretos de Desenlace.

Orden de búsqueda de los secretos de Telegram:

1. Llavero del sistema (``keyring``), preferido.
2. Variables de entorno.
3. Archivo ``.env`` en la carpeta del proyecto (alternativa).

Los valores nunca se imprimen. Las opciones no secretas se leen de variables
``DESENLACE_*`` (también admitidas en ``.env``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

KEYRING_SERVICE = "desenlace"
SECRET_NAMES = ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_STRING")
def _env_file() -> Path:
    """Primer .env que exista: el de la carpeta actual y después el del proyecto."""
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if candidate.is_file():
            return candidate
    return Path.cwd() / ".env"


ENV_FILE = _env_file()

_TRUE = {"1", "true", "yes", "on", "si", "sí"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(ValueError):
    """Valor de configuración no válido."""


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Lee un .env sencillo (KEY=VALUE). No modifica el entorno."""
    env_path = path or _env_file()
    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def _keyring_get(name: str) -> str | None:
    try:
        import keyring
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, name) or None
    except Exception:
        # Sin backend de llavero disponible (p. ej. Linux sin servicio de
        # secretos): se continúa con el resto de fuentes.
        return None


def get_secret(name: str, *, dotenv: Mapping[str, str] | None = None) -> str | None:
    """Devuelve un secreto desde llavero, entorno o .env (en ese orden)."""
    value = _keyring_get(name)
    if value:
        return value
    value = os.environ.get(name, "").strip()
    if value:
        return value
    source = dotenv if dotenv is not None else load_dotenv()
    value = (source.get(name) or "").strip()
    return value or None


def secret_source(name: str, *, dotenv: Mapping[str, str] | None = None) -> str | None:
    """Indica de dónde viene un secreto (sin revelar su valor)."""
    if _keyring_get(name):
        return "llavero"
    if os.environ.get(name, "").strip():
        return "entorno"
    source = dotenv if dotenv is not None else load_dotenv()
    if (source.get(name) or "").strip():
        return ".env"
    return None


def store_secret(name: str, value: str) -> None:
    """Guarda un secreto en el llavero del sistema."""
    if name not in SECRET_NAMES:
        raise ConfigError(f"Nombre de secreto no permitido: {name}")
    try:
        import keyring
    except ImportError as exc:
        raise ConfigError("Falta el paquete 'keyring'. Ejecuta: pip install keyring") from exc
    try:
        keyring.set_password(KEYRING_SERVICE, name, value)
    except Exception as exc:
        raise ConfigError(
            "No hay un llavero del sistema disponible. Usa el archivo .env "
            "como alternativa."
        ) from exc


def _bool_setting(name: str, default: bool, dotenv: Mapping[str, str]) -> bool:
    raw = os.environ.get(name, dotenv.get(name, "")).strip().lower()
    if raw == "":
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} debe ser 0 o 1.")


def _int_setting(name: str, default: int, minimum: int, dotenv: Mapping[str, str]) -> int:
    raw = os.environ.get(name, dotenv.get(name, "")).strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} debe ser un número entero.") from exc
    if value < minimum:
        raise ConfigError(f"{name} debe ser al menos {minimum}.")
    return value


def _float_setting(name: str, default: float, minimum: float, dotenv: Mapping[str, str]) -> float:
    raw = os.environ.get(name, dotenv.get(name, "")).strip()
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} debe ser un número.") from exc
    if value < minimum:
        raise ConfigError(f"{name} debe ser al menos {minimum:g}.")
    return value


@dataclass(frozen=True)
class Settings:
    """Opciones no secretas. Los argumentos de la CLI tienen prioridad."""

    max_redirects: int = 10
    browser_timeout: float = 120.0
    allow_private: bool = False


def load_settings(dotenv: Mapping[str, str] | None = None) -> Settings:
    values = dotenv if dotenv is not None else load_dotenv()
    return Settings(
        max_redirects=_int_setting("DESENLACE_MAX_REDIRECTS", 10, 1, values),
        browser_timeout=_float_setting("DESENLACE_BROWSER_TIMEOUT", 120.0, 5.0, values),
        allow_private=_bool_setting("DESENLACE_ALLOW_PRIVATE", False, values),
    )
