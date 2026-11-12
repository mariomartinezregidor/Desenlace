"""Línea de comandos de Desenlace.

Uso básico:  desenlace "https://enlace"
Subcomandos: desenlace setup      (configuración guiada)
             desenlace --diagnose "https://enlace"
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config
from desenlace_core import (
    CACHE_FILE,
    DEFAULT_MAX_HOPS,
    DEFAULT_MAX_WAIT,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DEFAULT_WAIT_BUFFER,
    ResolveError,
    redact_url,
)
from models import JobResult
from report import render_report, status_of
from orchestrator import Hooks, ProcessOptions, process_url
from plugins import PLUGINS
from resolver import ResolutionCache, load_cookie_file

def read_urls_file(path: str | Path) -> list[str]:
    """Lee URLs de un archivo de texto, una por línea (o separadas por espacios)."""
    file_path = Path(path)
    urls: list[str] = []
    with file_path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for token in line.split():
                if not token.startswith(("http://", "https://")):
                    raise ResolveError(
                        f"URL inválida en {file_path}:{line_number}: {token!r}"
                    )
                urls.append(token)
    if not urls:
        raise ResolveError(f"No se encontraron URLs en {file_path}.")
    return urls


def dedupe(urls: list[str]) -> list[str]:
    """Elimina enlaces repetidos conservando el orden."""
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def collect_urls(positional: list[str], files: list[str] | None) -> list[str]:
    """Combina URLs pasadas por argumento con las de uno o varios archivos."""
    urls: list[str] = []
    for item in positional:
        candidate = Path(item).expanduser()
        if candidate.is_file():
            urls.extend(read_urls_file(candidate))
        elif item.startswith(("http://", "https://")):
            urls.append(item)
        else:
            raise ResolveError(f"No es una URL ni un archivo existente: {item}")
    for file_name in files or []:
        urls.extend(read_urls_file(file_name))
    urls = dedupe(urls)
    if not urls:
        raise ResolveError("No se indicó ninguna URL.")
    return urls



def parse_extra_headers(items: list[str] | None) -> dict[str, str]:
    """Convierte `['Nombre: valor', ...]` en un diccionario de cabeceras."""
    headers: dict[str, str] = {}
    for item in items or []:
        if ":" not in item:
            raise ResolveError(f"Cabecera inválida (falta ':'): {item!r}")
        name, value = item.split(":", 1)
        name = name.strip()
        if not name:
            raise ResolveError(f"Cabecera inválida: {item!r}")
        headers[name] = value.strip()
    return headers


def get_version() -> str:
    """Obtiene la versión instalada, con respaldo al pyproject.toml del repo."""
    try:
        from importlib.metadata import version as _version

        return _version("desenlace")
    except Exception:
        pass
    try:
        import tomllib

        with (Path(__file__).resolve().parent / "pyproject.toml").open("rb") as handle:
            return tomllib.load(handle)["project"]["version"]
    except Exception:
        return "0.0.0"


def parse_size(value: str) -> int:
    """Convierte '100', '100MB', '1.5GB' en bytes."""
    import re

    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", value or "")
    if not match:
        raise argparse.ArgumentTypeError(f"Tamaño no válido: {value!r}")
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    factors = {
        "": 1,
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "m": 1024 ** 2,
        "mb": 1024 ** 2,
        "g": 1024 ** 3,
        "gb": 1024 ** 3,
        "t": 1024 ** 4,
        "tb": 1024 ** 4,
    }
    if unit not in factors:
        raise argparse.ArgumentTypeError(f"Unidad no reconocida: {unit!r}")
    return int(number * factors[unit])


def build_parser() -> argparse.ArgumentParser:
    settings_hint = " (por defecto: DESENLACE_MAX_REDIRECTS o 10)"
    parser = argparse.ArgumentParser(
        prog="desenlace",
        description=(
            "Resuelve enlaces con anuncios o pasos intermedios y descarga el archivo final. "
            "Subcomandos: 'setup' (configuración guiada)."
        ),
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="URL(s) de inicio o archivo(s) .txt con una URL por línea",
    )
    parser.add_argument(
        "-f", "--file", action="append", metavar="ARCHIVO",
        help="Archivo con URLs, una por línea. Se puede repetir.",
    )
    parser.add_argument(
        "--diagnose", action="store_true",
        help="Analiza paso a paso el enlace indicado y explica por qué se resuelve (o no)",
    )
    parser.add_argument(
        "--info", action="store_true",
        help="Solo mostrar el enlace final, sin descargar",
    )
    parser.add_argument(
        "-o", "--output",
        help="Archivo de destino o carpeta (por defecto ./downloads)",
    )
    parser.add_argument(
        "--full-chain", action="store_true",
        help="Seguir el wrapper /api/shorten en vez de extraer el destino directo",
    )
    parser.add_argument(
        "--browser", action="store_true",
        help="Usar un navegador headless (Chromium) desde el principio",
    )
    parser.add_argument(
        "--auto-browser", action="store_true",
        help="Si el modo normal falla, reintentar automáticamente con navegador",
    )
    parser.add_argument("--force", action="store_true", help="Sobrescribir archivos existentes")
    parser.add_argument("--verbose", "-v", action="store_true", help="Mostrar detalles adicionales")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help=f"Timeout por petición en segundos (por defecto {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--max-wait", type=float, default=DEFAULT_MAX_WAIT,
        help=f"Espera máxima permitida por temporizador (por defecto {DEFAULT_MAX_WAIT:g})",
    )
    parser.add_argument(
        "--max-hops", type=int, default=DEFAULT_MAX_HOPS,
        help=f"Máximo de saltos (por defecto {DEFAULT_MAX_HOPS})",
    )
    parser.add_argument(
        "--max-redirects", type=int, default=None, metavar="N",
        help="Límite de redirecciones HTTP por petición" + settings_hint,
    )
    parser.add_argument(
        "--allow-private", action="store_true",
        help="Permitir IPs privadas o locales. Desactiva la protección: sólo para pruebas",
    )
    parser.add_argument("--list-plugins", action="store_true", help="Mostrar los módulos de sitios cargados y salir")
    parser.add_argument(
        "-j", "--jobs", type=int, default=0, metavar="N",
        help="Enlaces resueltos en paralelo (0 = automático: hasta 4)",
    )
    parser.add_argument(
        "--wait-buffer", type=float, default=DEFAULT_WAIT_BUFFER, metavar="SEGUNDOS",
        help=f"Margen extra tras cada temporizador del servidor (por defecto {DEFAULT_WAIT_BUFFER:g} s)",
    )
    parser.add_argument("--no-cache", action="store_true", help="No leer ni escribir la caché de resoluciones")
    parser.add_argument("--refresh", action="store_true", help="Ignorar la caché y resolver de nuevo")
    parser.add_argument(
        "--cache-ttl", type=float, default=168.0, metavar="HORAS",
        help="Validez de la caché en horas (por defecto 168)",
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help="No descargar si el archivo ya existe con contenido",
    )
    parser.add_argument(
        "--retries", type=int, default=DEFAULT_RETRIES, metavar="N",
        help=f"Reintentos ante errores de red o HTTP 429/5xx (por defecto {DEFAULT_RETRIES})",
    )
    parser.add_argument("--user-agent", metavar="UA", help="User-Agent a usar en las peticiones")
    parser.add_argument(
        "--cookies", metavar="ARCHIVO",
        help="Archivo de cookies (formato Netscape o 'nombre=valor')",
    )
    parser.add_argument(
        "-H", "--header", action="append", default=[], metavar="CABECERA",
        help="Cabecera HTTP extra, por ejemplo 'Accept-Language: es'. Se puede repetir.",
    )
    parser.add_argument(
        "--version", action="version", version=f"desenlace {get_version()}"
    )
    parser.add_argument(
        "--max-size", type=parse_size, default=None, metavar="TAMAÑO",
        help="Tamaño máximo del archivo a descargar (ej. 100MB). Aborta si se supera.",
    )
    return parser


def _warn(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _resolve_settings(args: argparse.Namespace) -> config.Settings:
    settings = config.load_settings()
    if args.max_redirects is None:
        args.max_redirects = settings.max_redirects
    args.allow_private = bool(args.allow_private or settings.allow_private)
    if args.allow_private:
        _warn(
            "AVISO: --allow-private activo. La protección contra redes locales "
            "está desactivada (los endpoints de metadatos siguen bloqueados)."
        )
    return settings


def _options_for(
    args: argparse.Namespace,
    settings: config.Settings,
    *,
    cache: ResolutionCache | None,
    as_dir: bool,
) -> ProcessOptions:
    try:
        headers = parse_extra_headers(args.header)
        cookies = load_cookie_file(args.cookies) if args.cookies else None
    except ResolveError as exc:
        raise SystemExit(f"Error en las opciones: {exc}") from exc
    return ProcessOptions(
        output=args.output,
        as_dir=as_dir,
        force=args.force,
        info_only=args.info,
        browser=args.browser,
        auto_browser=args.auto_browser,
        skip_existing=args.skip_existing,
        use_cache=not args.refresh,
        timeout=args.timeout,
        max_wait=args.max_wait,
        max_hops=args.max_hops,
        max_redirects=args.max_redirects,
        browser_timeout=settings.browser_timeout,
        verbose=args.verbose,
        full_chain=args.full_chain,
        wait_buffer=args.wait_buffer,
        retries=args.retries,
        user_agent=args.user_agent,
        headers=headers,
        cookies=cookies,
        allow_private=args.allow_private,
        cache=cache,
        max_size=getattr(args, 'max_size', None),
    )


def _print_plugins() -> int:
    for plugin in PLUGINS:
        hosts = ", ".join(plugin.hosts) if plugin.hosts else "-"
        capabilities = []
        if plugin.detect is not None:
            capabilities.append("detect")
        if plugin.detect_url is not None:
            capabilities.append("url")
        if plugin.normalize is not None:
            capabilities.append("normalize")
        if plugin.browser_plan is not None:
            capabilities.append("plan")
        print(
            f"{plugin.priority:>3}  {plugin.name:<22} "
            f"[{'/'.join(capabilities) or 'catálogo'}] {hosts}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "setup":
        from setup_wizard import run_setup

        return run_setup(argv[1:])

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_plugins:
        return _print_plugins()

    try:
        settings = _resolve_settings(args)
    except config.ConfigError as exc:
        _warn(f"Error de configuración: {exc}")
        return 2

    if args.diagnose:
        from diagnose import run_diagnose

        try:
            targets = collect_urls(args.urls, args.file)
        except (OSError, ResolveError) as exc:
            _warn(f"Error leyendo el enlace: {exc}")
            return 1
        if len(targets) != 1:
            _warn("--diagnose analiza un único enlace.")
            return 1
        options = _options_for(args, settings, cache=None, as_dir=True)
        return run_diagnose(targets[0], options)

    try:
        urls = collect_urls(args.urls, args.file)
    except (OSError, ResolveError) as exc:
        _warn(f"Error leyendo los enlaces: {exc}")
        return 1

    as_dir = True
    if (
        args.output
        and len(urls) == 1
        and not str(args.output).endswith(("/", os.sep))
        and not Path(args.output).is_dir()
    ):
        as_dir = False

    jobs = args.jobs if args.jobs > 0 else (1 if len(urls) == 1 else min(4, len(urls)))
    jobs = max(1, min(jobs, len(urls)))

    cache = ResolutionCache(
        CACHE_FILE,
        ttl=max(0.0, args.cache_ttl) * 3600,
        enabled=not args.no_cache,
    )
    try:
        options = _options_for(args, settings, cache=cache, as_dir=as_dir)
    except SystemExit as exc:
        _warn(str(exc))
        return 1

    log_lock = threading.Lock()
    hooks = Hooks(
        log=lambda line: _emit(line, log_lock),
        log_lock=log_lock,
    )
    total = len(urls)

    def run_job(index: int, start_url: str) -> JobResult:
        return process_url(start_url, options, hooks, index=index, total=total)

    try:
        if jobs == 1:
            results = [run_job(index, url) for index, url in enumerate(urls, 1)]
        else:
            with ThreadPoolExecutor(max_workers=jobs) as executor:
                futures = [
                    executor.submit(run_job, index, url)
                    for index, url in enumerate(urls, 1)
                ]
                results = [future.result() for future in futures]
    except KeyboardInterrupt:
        _warn("\nCancelado.")
        return 130

    return _report(results, args.info)


def _emit(line: str, lock: threading.Lock) -> None:
    with lock:
        print(line, file=sys.stderr, flush=True)


def _report(results: list[JobResult], info_only: bool) -> int:
    failures = sum(1 for job in results if status_of(job) == "ERROR")
    if info_only:
        # Salida pensada para scripts: una URL final por línea.
        for job in results:
            if job.resolution is not None:
                print(job.resolution.final_url)
            else:
                _warn(f"Error con {redact_url(job.start_url)}: {job.error or 'sin resultado'}")
        return 1 if failures else 0

    if len(results) == 1:
        job = results[0]
        if job.resolution is None:
            _warn(f"Error con {redact_url(job.start_url)}: {job.error or 'sin resultado'}")
            return 1
        if job.skipped:
            print(f"Omitido (ya existe): {job.destination} ({job.elapsed:.1f}s)")
            return 0
        size = job.destination.stat().st_size if job.destination else 0
        print(f"URL final: {job.resolution.final_url}")
        print(f"Archivo: {job.resolution.filename}")
        print(f"Descargado: {job.destination} ({size} bytes) ({job.elapsed:.1f}s)")
        return 0

    print()
    print(render_report(results))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
