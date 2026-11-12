"""Descarga del archivo final a disco, con reanudación.

Cada descarga se escribe en un fichero ``.part``. Si la conexión se corta, el
progreso se conserva y se continúa con una cabecera ``Range`` en lugar de
empezar de cero. Para no mezclar dos archivos distintos, sólo se reanuda si el
servidor dio un validador (``ETag`` o ``Last-Modified``) y se envía como
``If-Range``. Si el servidor ignora el rango o el archivo cambió, se reinicia.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import requests

from desenlace_core import (
    ResolveError,
    base_destination,
    discard_temp_file,
    ensure_extension,
    filename_from_disposition,
    filename_from_url,
    redact_url,
    release_destination,
    reserve_destination,
    reserve_exact,
    sanitize_filename,
)
from models import Resolution

ProgressCallback = Callable[[int, int], None]
LogCallback = Callable[[str], None]

CHUNK_SIZE = 65536
_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.I)
_META_LOCK = threading.Lock()


def _part_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".part")


def _meta_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".part.json")


def _read_meta(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(path: Path, data: dict) -> None:
    with _META_LOCK:
        try:
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass


def _validator(response: requests.Response) -> str | None:
    """Valor para If-Range: ETag fuerte o, si no hay, Last-Modified."""
    etag = response.headers.get("ETag")
    if etag and not etag.startswith("W/"):
        return etag
    return response.headers.get("Last-Modified") or None


def _open_stream(
    session: requests.Session,
    url: str,
    *,
    referer: str,
    timeout: float,
    offset: int,
    validator: str | None,
) -> tuple[requests.Response | None, int]:
    """Abre la descarga desde ``offset``.

    Devuelve ``(respuesta, desplazamiento_real)``. Si el servidor no puede
    continuar desde ahí devuelve ``(None, 0)`` y el llamante reinicia.
    """
    headers = {"Referer": referer, "Accept": "*/*", "Accept-Encoding": "identity"}
    if offset > 0:
        if not validator:
            return None, 0
        headers["Range"] = f"bytes={offset}-"
        headers["If-Range"] = validator
    response = session.get(
        url, headers=headers, timeout=timeout, allow_redirects=True, stream=True
    )
    if offset == 0:
        return response, 0
    if response.status_code == 206:
        match = _CONTENT_RANGE_RE.search(response.headers.get("Content-Range", ""))
        if match and int(match.group(1)) == offset:
            return response, offset
        response.close()
        return None, 0
    # 200: el servidor ignoró el rango (archivo cambiado o sin soporte).
    # 416: rango no válido. En ambos casos se reinicia desde cero.
    response.close()
    return None, 0


def _expected_total(response: requests.Response, offset: int) -> int:
    match = _CONTENT_RANGE_RE.search(response.headers.get("Content-Range", ""))
    if response.status_code == 206 and match and match.group(3) != "*":
        return int(match.group(3))
    length = int(response.headers.get("content-length") or 0)
    if response.status_code == 206:
        return offset + length
    return length


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
    resume_retries: int = 2,
) -> Path:
    """Descarga el archivo de ``target`` en ``output``.

    ``resume_retries`` indica cuántas veces se reanuda automáticamente tras un
    corte dentro de la misma ejecución. Si el proceso se detiene del todo, al
    volver a ejecutar el mismo comando la descarga se reanuda desde el ``.part``.
    """
    if target.prefetched is not None and target.prefetched.is_file():
        return _move_prefetched(target, output, as_dir=as_dir, force=force, log=log)

    referer = target.referer or target.final_url
    name = sanitize_filename(
        target.filename or filename_from_url(target.final_url) or "download.bin"
    )
    # Ruta sin resolver duplicados: aquí buscamos un .part de una descarga previa.
    resumable_base = base_destination(output, name, as_dir)
    part_resume = _part_path(resumable_base)
    meta_resume = _meta_path(resumable_base)

    offset = 0
    if not force and part_resume.is_file() and not resumable_base.exists():
        meta = _read_meta(meta_resume)
        if meta.get("url") == target.final_url and meta.get("validator"):
            offset = part_resume.stat().st_size
            validator = str(meta["validator"])
        else:
            validator = None
    else:
        validator = None

    response, offset = _open_stream(
        session, target.final_url, referer=referer, timeout=timeout,
        offset=offset, validator=validator,
    )
    if response is None:
        offset = 0
        response, _ = _open_stream(
            session, target.final_url, referer=referer, timeout=timeout,
            offset=0, validator=None,
        )
    assert response is not None

    destination: Path | None = None
    try:
        if response.status_code >= 400:
            raise ResolveError(
                f"HTTP {response.status_code} al descargar {redact_url(target.final_url)}"
            )
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type and offset == 0:
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

        if offset == 0:
            disposition_name = filename_from_disposition(
                response.headers.get("content-disposition")
            )
            final_name = sanitize_filename(
                ensure_extension(
                    target.filename or disposition_name or name,
                    response.headers.get("content-type"),
                )
            )
            base = base_destination(output, final_name, as_dir)
            if force or not base.exists():
                destination = reserve_exact(base)
            else:
                # Ya existe un archivo con ese nombre: se crea uno nuevo.
                destination = reserve_destination(output, final_name, as_dir=as_dir, force=False)
        else:
            destination = reserve_exact(resumable_base)
            log(f"    reanudando descarga desde el byte {offset}")

        part = _part_path(destination)
        meta_path = _meta_path(destination)
        total = _expected_total(response, offset)
        _write_meta(
            meta_path,
            {
                "url": target.final_url,
                "validator": _validator(response),
                "total": total,
            },
        )
        if offset == 0:
            part.unlink(missing_ok=True)
        _stream_to_part(
            session, response, part, offset=offset, total=total,
            progress=progress, log=log, referer=referer, timeout=timeout,
            target_url=target.final_url, validator=_validator(response),
            resume_retries=resume_retries, meta_path=meta_path,
        )
        part.replace(destination)
        meta_path.unlink(missing_ok=True)
        log(f"    guardado {destination} ({destination.stat().st_size} bytes)")
        return destination
    finally:
        if destination is not None:
            release_destination(destination)
        try:
            response.close()
        except Exception:
            pass


def _stream_to_part(
    session: requests.Session,
    response: requests.Response,
    part: Path,
    *,
    offset: int,
    total: int,
    progress: ProgressCallback | None,
    log: LogCallback,
    referer: str,
    timeout: float,
    target_url: str,
    validator: str | None,
    resume_retries: int,
    meta_path: Path,
) -> None:
    """Escribe el cuerpo en ``part``; si se corta, reanuda con Range."""
    mode = "ab" if offset > 0 else "wb"
    if offset == 0:
        part.parent.mkdir(parents=True, exist_ok=True)
    retries_left = max(0, resume_retries)
    attempt = 0
    last_print = 0.0
    while True:
        try:
            if progress is not None:
                progress(offset, total)
            with part.open(mode) as handle:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    offset += len(chunk)
                    if progress is not None:
                        progress(offset, total)
                    now = time.monotonic()
                    if sys.stderr.isatty() and total and now - last_print >= 0.5:
                        last_print = now
                        percent = min(100, offset * 100 // total)
                        print(
                            f"\r    descargando {percent:3d}% ({offset}/{total} bytes)",
                            end="",
                            file=sys.stderr,
                            flush=True,
                        )
            if sys.stderr.isatty() and total:
                print(file=sys.stderr)
            size = part.stat().st_size
            if total and size != total:
                raise requests.exceptions.ChunkedEncodingError(
                    f"descarga incompleta ({size}/{total} bytes)"
                )
            return
        except requests.RequestException as exc:
            response.close()
            offset = part.stat().st_size if part.exists() else 0
            if retries_left <= 0:
                raise ResolveError(
                    f"La descarga se interrumpió ({exc}). Se han conservado "
                    f"{offset} bytes: vuelve a ejecutar el mismo comando para reanudarla."
                ) from exc
            retries_left -= 1
            attempt += 1
            backoff = min(8.0, 0.6 * (2 ** (attempt - 1)))
            log(f"    descarga interrumpida en {offset} bytes; reanudando en {backoff:.1f}s...")
            time.sleep(backoff)
            reopened, real_offset = _open_stream(
                session, target_url, referer=referer, timeout=timeout,
                offset=offset, validator=validator,
            )
            if reopened is None:
                # El servidor no permite continuar: reinicia el archivo.
                log("    el servidor no permite reanudar; se descarga desde el principio")
                reopened, _ = _open_stream(
                    session, target_url, referer=referer, timeout=timeout,
                    offset=0, validator=None,
                )
                if reopened is None:
                    raise ResolveError("No se pudo reabrir la descarga.") from exc
                real_offset = 0
            if reopened.status_code >= 400:
                raise ResolveError(f"HTTP {reopened.status_code} al reanudar la descarga.") from exc
            response = reopened
            if real_offset == 0:
                part.unlink(missing_ok=True)
                offset = 0
                mode = "wb"
                total = _expected_total(response, 0) or total
            else:
                offset = real_offset
                mode = "ab"
            _write_meta(
                meta_path,
                {"url": target_url, "validator": validator, "total": total},
            )


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
    discard_temp_file(source)
    log(f"    guardado {destination} ({destination.stat().st_size} bytes, desde caché temporal)")
    return destination
