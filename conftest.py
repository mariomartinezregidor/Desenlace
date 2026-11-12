"""Configuración de pytest: omite las pruebas de navegador si no hay Chromium."""

from __future__ import annotations

from pathlib import Path

import pytest


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            path = playwright.chromium.executable_path
    except Exception:
        return False
    return bool(path) and Path(path).exists()


def pytest_collection_modifyitems(config, items):
    selected = [item for item in items if item.get_closest_marker("browser")]
    if not selected or _chromium_available():
        return
    reason = "Chromium de Playwright no instalado (python -m playwright install chromium)"
    skip = pytest.mark.skip(reason=reason)
    for item in selected:
        item.add_marker(skip)
