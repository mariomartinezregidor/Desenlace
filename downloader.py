"""Descarga del archivo final a disco.

Escribe primero en un fichero ``.part`` y lo renombra al terminar, así nunca
queda un archivo a medias con el nombre definitivo.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Callable

import requests

from desenlace_core import (
    ResolveError,
    discard_temp_file,
    ensure_extension,
    filename_from_disposition,
    filename_from_url,
    redact_url,
    release_destination,
    reserve_destination,
    sanitize_filename,
)
from models import Resolution

ProgressCallback = Callable[[int, int], None]
LogCallback = Callable[[str], None]


def download_resolution(
    session: requests.Session,
    target: Resolution,
    output: str | None = None,
    *,
    timeout: float,
    force: bool = False,
    as_dir: bool = True,
    progress: ProgressCallback | None = None,
    log: LogCallback = lambda message: None,
    max_redirects: int = 10,
) -> Path:
    """Descarga el archivo de ``target`` en ``output``.

    Si ``progress`` se indica, se llama con ``(descargado, total)`` en bytes
    (``total`` vale 0 si el servidor no informa del tamaño).
    """
    if target.prefetched is not None and target.prefetched.is_file():
        return _move_prefetched(target, output, as_dir=as_dir, force=force, log=log)

    headers = {
        "Referer": target.referer or target.final_url,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    destination: Path | None = None
    try:
        response = session.get(
            target.final_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
    except requests.TooManyRedirects as exc:
        raise ResolveError(
            f"Se superó el límite de {max_redirects} redirecciones "
            f"al descargar {redact_url(target.final_url)}"
        ) from exc

    try:
        if response.status_code >= 400:
            raise ResolveError(
                f"HTTP {response.status_code} al descargar {redact_url(target.final_url)}"
            )
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type:
            if target.page_url:
                destination = reserve_destination(
                    output, sanitize_filename("enlace.url"), as_dir=as_dir, force=force
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(
                    f"[InternetShortcut]\nURL={target.final_url}\n", encoding="utf-8"
                )
                log("    el destino final es HTML; guardado acceso directo .url")
                return destination
            snippet = response.text[:300]
            raise ResolveError(
                "El enlace de descarga devolvió HTML en vez del archivo "
                f"(posible login o bloqueo): {snippet!r}"
            )

        disposition_name = filename_from_disposition(
            response.headers.get("content-disposition")
        )
        filename = sanitize_filename(
            ensure_extension(
                disposition_name
                or target.filename
                or filename_from_url(response.url)
                or "download.bin",
                response.headers.get("content-type"),
            )
        )
        destination = reserve_destination(output, filename, as_dir=as_dir, force=force)
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = Path(str(destination) + ".part")
        total = int(response.headers.get("content-length") or 0)
        downloaded = 0
        last_print = 0.0
        if progress is not None:
            progress(0, total)

        try:
            with part.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress is not None:
                        progress(downloaded, total)
                    now = time.monotonic()
                    if sys.stderr.isatty() and total and now - last_print >= 0.5:
                        last_print = now
                        percent = min(100, downloaded * 100 // total)
                        print(
                            f"\r    descargando {percent:3d}% "
                            f"({downloaded}/{total} bytes)",
                            end="",
                            file=sys.stderr,
                            flush=True,
                        )
            if sys.stderr.isatty() and total:
                print(file=sys.stderr)
            if total and downloaded != total:
                raise ResolveError(f"Descarga incompleta: {downloaded}/{total} bytes.")
            part.replace(destination)
        except BaseException:
            part.unlink(missing_ok=True)
            raise

        log(f"    guardado {destination} ({downloaded} bytes)")
        return destination
    finally:
        if destination is not None:
            release_destination(destination)
        response.close()


def _move_prefetched(
    target: Resolution,
    output: str | None,
    *,
    as_dir: bool,
    force: bool,
    log: LogCallback,
) -> Path:
    """Mueve un archivo que ya trajo el navegador o Telegram al destino final."""
    source = target.prefetched
    assert source is not None
    filename = sanitize_filename(target.filename or source.name)
    destination = reserve_destination(output, filename, as_dir=as_dir, force=force)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(source), str(destination))
    except Exception:
        release_destination(destination)
        raise
    release_destination(destination)
    discard_temp_file(source)  # elimina la carpeta temporal si quedó vacía
    log(f"    guardado {destination} ({destination.stat().st_size} bytes, desde caché temporal)")
    return destination
