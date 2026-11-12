"""Tipos de datos compartidos por el resolvedor, la descarga y las interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Resolution:
    """Resultado de resolver un enlace: de dónde descargar y cómo nombrarlo."""

    final_url: str
    filename: str
    referer: str
    hops: int
    page_url: str | None = None
    # Archivo ya descargado por el navegador o por Telegram, pendiente de
    # moverse al destino definitivo cuando se llame a la descarga.
    prefetched: Path | None = None


@dataclass
class JobResult:
    """Resultado final de procesar un enlace (éxito, omitido o error)."""

    index: int
    start_url: str
    resolution: Resolution | None = None
    destination: Path | None = None
    skipped: bool = False
    error: Exception | None = None
    elapsed: float = 0.0
    trace: list[dict[str, Any]] = field(default_factory=list)
