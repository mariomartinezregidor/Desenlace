"""Tabla resumen para procesar varios enlaces a la vez.

Sólo usa caracteres ASCII para que se vea bien también en la consola de Windows.
Los enlaces se muestran sin valores sensibles.
"""

from __future__ import annotations

from typing import Sequence

from desenlace_core import redact_url
from models import JobResult

HEADERS = ("#", "Enlace", "Estado", "Archivo", "Motivo")
MAX_LINK = 42
MAX_FILE = 28
MAX_REASON = 56


def _shorten(text: str, limit: int) -> str:
    """Acorta por el centro para conservar el inicio y el final del texto."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    keep = limit - 3
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}...{text[-tail:]}"


def status_of(job: JobResult) -> str:
    if job.error is not None or job.resolution is None:
        return "ERROR"
    if job.skipped:
        return "OMITIDO"
    return "OK"


def build_rows(results: Sequence[JobResult]) -> list[list[str]]:
    rows: list[list[str]] = []
    for job in results:
        status = status_of(job)
        if status == "ERROR":
            reason = str(job.error) if job.error is not None else "sin resultado"
            filename = "-"
        else:
            reason = ""
            if status == "OMITIDO":
                reason = "ya existía"
            filename = job.destination.name if job.destination else (
                job.resolution.filename if job.resolution else "-"
            )
        rows.append(
            [
                str(job.index),
                _shorten(redact_url(job.start_url), MAX_LINK),
                status,
                _shorten(filename or "-", MAX_FILE),
                _shorten(reason or "-", MAX_REASON),
            ]
        )
    return rows


def format_table(rows: Sequence[Sequence[str]]) -> str:
    """Tabla con bordes ASCII. ``rows`` no incluye la cabecera."""
    table = [list(HEADERS), *[list(row) for row in rows]]
    widths = [max(len(row[col]) for row in table) for col in range(len(HEADERS))]

    def line(char: str = "-") -> str:
        return "+" + "+".join(char * (width + 2) for width in widths) + "+"

    def render(row: Sequence[str]) -> str:
        cells = [f" {value.ljust(width)} " for value, width in zip(row, widths)]
        return "|" + "|".join(cells) + "|"

    out = [line(), render(table[0]), line("=")]
    out.extend(render(row) for row in table[1:])
    out.append(line())
    return "\n".join(out)


def summary_line(results: Sequence[JobResult]) -> str:
    counts = {"OK": 0, "OMITIDO": 0, "ERROR": 0}
    for job in results:
        counts[status_of(job)] += 1
    total = len(results)
    return (
        f"Total: {total}  ·  OK: {counts['OK']}  ·  "
        f"Omitidos: {counts['OMITIDO']}  ·  Errores: {counts['ERROR']}"
    )


def render_report(results: Sequence[JobResult]) -> str:
    return format_table(build_rows(results)) + "\n" + summary_line(results)
