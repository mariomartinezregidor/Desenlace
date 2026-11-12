"""Asistente de configuración: ``desenlace setup``.

Comprueba la instalación, instala el navegador si falta y guarda los datos de
Telegram en el llavero del sistema. Nunca muestra los valores secretos.
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import platform
import subprocess
import sys

import config

REQUIRED_MODULES = ("requests", "bs4", "playwright")
OPTIONAL_MODULES = ("telethon", "keyring")

SECRET_LABELS = {
    "TELEGRAM_API_ID": "API ID de Telegram",
    "TELEGRAM_API_HASH": "API Hash de Telegram",
    "TELEGRAM_SESSION_STRING": "Sesión de Telegram",
}


def _check_python() -> bool:
    ok = sys.version_info >= (3, 10)
    version = platform.python_version()
    print(f"[{'OK' if ok else 'ERROR'}] Python {version} (se requiere 3.10 o superior)")
    return ok


def _check_modules() -> bool:
    all_ok = True
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
            print(f"[OK] {name}")
        except ImportError:
            all_ok = False
            print(f"[FALTA] {name}  →  pip install -r requirements.txt")
    for name in OPTIONAL_MODULES:
        try:
            importlib.import_module(name)
            print(f"[OK] {name} (opcional)")
        except ImportError:
            print(f"[OPCIONAL] {name} no instalado")
    return all_ok


def _browser_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            path = playwright.chromium.executable_path
    except Exception:
        return False
    from pathlib import Path

    return bool(path) and Path(path).exists()


def _install_browser(assume_yes: bool, interactive: bool) -> bool:
    if _browser_installed():
        print("[OK] Navegador Chromium instalado")
        return True
    print("[FALTA] Navegador Chromium (necesario para --auto-browser)")
    if not assume_yes:
        if not interactive:
            print("      Ejecuta: python3 -m playwright install chromium")
            return False
        answer = input("      ¿Instalarlo ahora (unos 130 MB)? [s/N] ").strip().lower()
        if answer not in ("s", "si", "sí", "y", "yes"):
            return False
    print("      Instalando Chromium…")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False,
    )
    ok = result.returncode == 0
    print("[OK] Chromium instalado" if ok else "[ERROR] La instalación del navegador falló")
    return ok


def _telegram(assume_yes: bool, interactive: bool) -> None:
    print("\nTelegram (sólo necesario para bots de Telegram):")
    dotenv = config.load_dotenv()
    for name in config.SECRET_NAMES:
        source = config.secret_source(name, dotenv=dotenv)
        label = SECRET_LABELS[name]
        if source:
            print(f"[OK] {label}: configurado (origen: {source})")
            continue
        print(f"[FALTA] {label}")
        if assume_yes or not interactive:
            continue
        answer = input("      ¿Guardarlo ahora en el llavero? [s/N] ").strip().lower()
        if answer not in ("s", "si", "sí", "y", "yes"):
            continue
        value = getpass.getpass("      Pega el valor (no se muestra): ").strip()
        if not value:
            print("      Valor vacío; se omite.")
            continue
        try:
            config.store_secret(name, value)
            print("      [OK] Guardado en el llavero del sistema.")
        except config.ConfigError as exc:
            print(f"      [ERROR] {exc}")
            print(f"      Alternativa: añade {name}=<valor> en el archivo .env")
        finally:
            value = ""  # no conservar el secreto en memoria más de lo necesario


def run_setup(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="desenlace.py setup", description="Configuración guiada.")
    parser.add_argument("--yes", "-y", action="store_true", help="Responder sí a todo (sin preguntas)")
    parser.add_argument("--skip-browser", action="store_true", help="No comprobar ni instalar el navegador")
    parser.add_argument("--skip-telegram", action="store_true", help="No configurar Telegram")
    args = parser.parse_args(argv)
    interactive = sys.stdin.isatty()

    print("Configuración de Desenlace\n")
    print("Comprobando el sistema…")
    ok = _check_python()
    ok = _check_modules() and ok
    if not args.skip_browser:
        print("\nNavegador:")
        ok = _install_browser(args.yes, interactive) and ok
    if not args.skip_telegram:
        _telegram(args.yes, interactive)

    print()
    telegram_ready = all(config.secret_source(name) for name in config.SECRET_NAMES)
    if not ok:
        print("Hay pasos pendientes (marcados como FALTA o ERROR).")
        return 1
    print("Listo para resolver enlaces. Prueba con:")
    print('  desenlace --info "https://enlace"')
    if not telegram_ready and not args.skip_telegram:
        print("Telegram no está configurado: los bots de Telegram no funcionarán.")
        print("Los canales públicos y el resto de enlaces sí.")
    return 0
