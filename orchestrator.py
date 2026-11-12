"""Orquestador: procesa un enlace de principio a fin.

Es el único sitio que decide el recorrido de un enlace: HTTP, navegador,
reintento automático con navegador, sólo-enlace, omitir existente y
cancelación. La línea de comandos y la interfaz web sólo construyen las
opciones y reciben el ``JobResult``. Así ambas se comportan igual.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from desenlace_core import (
    DEFAULT_MAX_HOPS,
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_MAX_WAIT,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DEFAULT_WAIT_BUFFER,
    ResolveError,
    base_destination,
    discard_temp_file,
    redact_url,
    sanitize_filename,
)
from models import JobResult, Resolution
from resolver import LinkResolver, ResolutionCache, browser_resolve_and_download

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]


@dataclass
class ProcessOptions:
    """Opciones de procesamiento. Cada interfaz rellena las que necesite."""

    output: str | None = None
    as_dir: bool = True
    force: bool = False
    info_only: bool = False
    browser: bool = False
    auto_browser: bool = False
    skip_existing: bool = False
    use_cache: bool = True
    timeout: float = DEFAULT_TIMEOUT
    max_wait: float = DEFAULT_MAX_WAIT
    max_hops: int = DEFAULT_MAX_HOPS
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    browser_timeout: float = 120.0
    verbose: bool = False
    full_chain: bool = False
    wait_buffer: float = DEFAULT_WAIT_BUFFER
    retries: int = DEFAULT_RETRIES
    user_agent: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    cookies: Any = None
    allow_private: bool = False
    cache: ResolutionCache | None = None
    max_size: int | None = None


@dataclass
class Hooks:
    """Puntos de conexión con la interfaz (logs, progreso, cancelación)."""

    log: LogCallback | None = None
    progress: ProgressCallback | None = None
    cancelled: Callable[[], bool] | None = None
    on_filename: Callable[[str], None] | None = None
    log_lock: threading.Lock | None = None


def build_resolver(
    options: ProcessOptions,
    hooks: Hooks,
    *,
    prefix: str = "",
    trace: list[dict[str, Any]] | None = None,
) -> LinkResolver:
    return LinkResolver(
        timeout=options.timeout,
        max_wait=options.max_wait,
        max_hops=options.max_hops,
        max_redirects=options.max_redirects,
        verbose=options.verbose,
        full_chain=options.full_chain,
        wait_buffer=options.wait_buffer,
        cache=options.cache,
        log_lock=hooks.log_lock,
        prefix=prefix,
        user_agent=options.user_agent,
        headers=dict(options.headers),
        cookies=options.cookies,
        retries=options.retries,
        allow_private=options.allow_private,
        browser_fallback=options.auto_browser,
        logger=hooks.log,
        trace=trace,
        browser_timeout=options.browser_timeout,
        max_size=options.max_size,
    )


def process_url(
    url: str,
    options: ProcessOptions,
    hooks: Hooks | None = None,
    *,
    index: int = 1,
    total: int = 1,
    trace: list[dict[str, Any]] | None = None,
) -> JobResult:
    """Procesa un enlace y devuelve un ``JobResult``. Nunca lanza excepciones."""
    hooks = hooks or Hooks()
    prefix = f"[{index}/{total}] " if total > 1 else ""
    started = time.monotonic()
    steps: list[dict[str, Any]] = trace if trace is not None else []
    resolver = build_resolver(options, hooks, prefix=prefix, trace=steps)

    def elapsed() -> float:
        return time.monotonic() - started

    def check_cancelled() -> None:
        if hooks.cancelled is not None and hooks.cancelled():
            raise ResolveError("Descarga cancelada por el usuario.")

    def progress(downloaded: int, total_bytes: int) -> None:
        check_cancelled()
        if hooks.progress is not None:
            hooks.progress(downloaded, total_bytes)

    def announce(name: str) -> None:
        if hooks.on_filename is not None:
            hooks.on_filename(name)

    def attempt_browser() -> tuple[Resolution, Path | None, bool]:
        check_cancelled()
        final_url, filename, destination = browser_resolve_and_download(
            url,
            options.output,
            force=options.force,
            as_dir=options.as_dir,
            timeout=options.timeout,
            max_wait=options.max_wait,
            verbose=options.verbose,
            save=not options.info_only,
            user_agent=options.user_agent,
            resolver=resolver,
            max_sequence_seconds=options.browser_timeout,
            allow_private=options.allow_private,
        )
        result = Resolution(
            final_url=final_url,
            filename=sanitize_filename(filename),
            referer=url,
            hops=0,
        )
        announce(result.filename)
        if options.info_only:
            discard_temp_file(destination)
            return result, None, False
        if destination is None:
            destination = resolver.download(
                result, options.output, force=options.force, as_dir=options.as_dir,
                progress=progress,
                max_size=options.max_size,
            )
        if (
            destination is not None
            and options.max_size is not None
            and destination.is_file()
            and destination.stat().st_size > options.max_size
        ):
            try:
                destination.unlink()
            except OSError:
                pass
            raise ResolveError(
                f"El archivo supera el tamaño máximo permitido ({options.max_size} bytes)."
            )
        return result, destination, False

    def attempt_http() -> tuple[Resolution, Path | None, bool]:
        check_cancelled()
        result = resolver.resolve(url, use_cache=options.use_cache)
        announce(result.filename)
        if options.info_only:
            discard_temp_file(result.prefetched)
            return result, None, False
        if options.skip_existing and not options.force:
            candidate = base_destination(options.output, result.filename, options.as_dir)
            if candidate.is_file() and candidate.stat().st_size > 0:
                if options.max_size is not None and candidate.stat().st_size > options.max_size:
                    raise ResolveError(
                        f"El archivo existente supera el tamaño máximo "
                        f"permitido ({options.max_size} bytes)."
                    )
                discard_temp_file(result.prefetched)
                return result, candidate, True
        destination = resolver.download(
            result, options.output, force=options.force, as_dir=options.as_dir,
            progress=progress,
            max_size=options.max_size,
        )
        if (
            destination is not None
            and options.max_size is not None
            and destination.is_file()
            and destination.stat().st_size > options.max_size
        ):
            try:
                destination.unlink()
            except OSError:
                pass
            raise ResolveError(
                f"El archivo supera el tamaño máximo permitido ({options.max_size} bytes)."
            )
        return result, destination, False

    def attempt(use_browser: bool) -> tuple[Resolution, Path | None, bool]:
        return attempt_browser() if use_browser else attempt_http()

    try:
        result, destination, skipped = attempt(options.browser)
    except Exception as exc:
        cancelled = hooks.cancelled is not None and hooks.cancelled()
        if not (options.auto_browser and not options.browser) or cancelled:
            return JobResult(index, url, error=exc, elapsed=elapsed(), trace=steps)
        if hooks.log is not None:
            hooks.log(f"{prefix}    HTTP falló ({exc}); reintentando con navegador...")
        try:
            result, destination, skipped = attempt_browser()
        except Exception as second:
            return JobResult(index, url, error=second, elapsed=elapsed(), trace=steps)

    return JobResult(
        index,
        url,
        result,
        destination,
        skipped=skipped,
        elapsed=elapsed(),
        trace=steps,
    )


def safe_display(url: str) -> str:
    """URL lista para mostrar en logs (sin valores sensibles)."""
    return redact_url(url)
