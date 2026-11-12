#!/usr/bin/env python3
"""Desenlace: interfaz web local para resolver enlaces y descargar los archivos.

Sirve una página HTML en 127.0.0.1 con un cuadro de texto para pegar los
enlaces, un botón que abre el diálogo nativo para elegir la carpeta de destino
y barras de progreso de la descarga. Incluye un modo de solo enlace final que
resuelve sin descargar y permite copiar los resultados.

Uso:
    python3 desenlace_web.py
    python3 desenlace_web.py --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, TypedDict
from urllib.parse import parse_qs, urlsplit

BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "index.html"

# Permite importar desenlace.py aunque se lance desde otra carpeta.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from desenlace import (  # noqa: E402
    CACHE_FILE,
    DEFAULT_CACHE_TTL,
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_RETRIES,
    LinkResolver,
    Resolution,
    ResolutionCache,
    ResolveError,
    browser_resolve_and_download,
    load_cookie_file,
    parse_extra_headers,
    redact_url,
)

MAX_LOG_LINES = 400
MAX_REQUEST_BODY = 1_000_000
JOB_RETENTION_SECONDS = 3600
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

# Diálogo nativo de selección de carpeta (tkinter en un subproceso aislado).
FOLDER_DIALOG_SCRIPT = """
import sys
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception:
    sys.exit(3)
try:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askdirectory(title="Selecciona la carpeta de descarga")
    root.destroy()
except Exception:
    sys.exit(4)
print(path or "")
"""


def parse_links(text: str) -> list[str]:
    """Convierte el texto pegado en una lista de URLs únicas y válidas."""
    urls: list[str] = []
    vistos: set[str] = set()
    for numero, linea in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        for token in linea.split():
            if not token.startswith(("http://", "https://")):
                raise ResolveError(
                    f"Enlace no válido en la línea {numero}: {redact_url(token)}"
                )
            if token not in vistos:
                vistos.add(token)
                urls.append(token)
    if not urls:
        raise ResolveError("No se indicó ningún enlace.")
    return urls


def elegir_carpeta() -> str:
    """Abre el diálogo nativo de carpetas y devuelve la ruta elegida."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise ResolveError(
            "No hay entorno gráfico disponible para abrir el selector de carpetas."
        )
    try:
        proceso = subprocess.run(
            [sys.executable, "-c", FOLDER_DIALOG_SCRIPT],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise ResolveError("El selector de carpetas tardó demasiado.") from exc
    if proceso.returncode != 0:
        raise ResolveError("No se pudo abrir el selector de carpetas del sistema.")
    return proceso.stdout.strip()


class Resultado(TypedDict):
    """Resultado serializable de un enlace procesado."""

    indice: int
    enlace: str
    archivo: str
    final: str
    estado: str
    error: str
    transcurrido: float


class Job:
    """Estado compartido de una descarga en curso."""

    def __init__(self, total: int) -> None:
        self.id = uuid.uuid4().hex
        self.total = total
        self.completados = 0
        self.fallidos = 0
        self.estado = "en_curso"
        self.indice = 0
        self.enlace = ""
        self.archivo = ""
        self.descargado = 0
        self.total_bytes = 0
        self.inicio = time.monotonic()
        self.cancelar = threading.Event()
        self._log: list[str] = []
        self._resultados: list[Resultado] = []
        self._lock = threading.Lock()

    def anadir_log(self, mensaje: str) -> None:
        with self._lock:
            self._log.append(mensaje)
            if len(self._log) > MAX_LOG_LINES:
                del self._log[: len(self._log) - MAX_LOG_LINES]

    def anadir_resultado(self, resultado: Resultado) -> None:
        with self._lock:
            self._resultados.append(resultado)

    def iniciar_enlace(self, indice: int, url: str) -> None:
        with self._lock:
            self.indice = indice
            self.enlace = redact_url(url)
            self.archivo = ""
            self.descargado = 0
            self.total_bytes = 0

    def actualizar_progreso(self, descargado: int, total: int) -> None:
        with self._lock:
            self.descargado = descargado
            self.total_bytes = total

    def registrar_archivo(self, nombre: str) -> None:
        with self._lock:
            self.archivo = nombre

    def registrar_exito(self) -> None:
        with self._lock:
            self.completados += 1

    def registrar_fallo(self) -> None:
        with self._lock:
            self.fallidos += 1

    def expirado(self, ahora: float) -> bool:
        with self._lock:
            return (
                self.estado != "en_curso"
                and ahora - self.inicio >= JOB_RETENTION_SECONDS
            )

    def resumen(self) -> tuple[int, int]:
        with self._lock:
            return self.completados, self.fallidos

    def finalizar(self) -> None:
        with self._lock:
            if self.cancelar.is_set():
                self.estado = "cancelado"
            elif self.fallidos:
                self.estado = "terminado_con_errores"
            else:
                self.estado = "terminado"

    def instantanea(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "estado": self.estado,
                "total": self.total,
                "completados": self.completados,
                "fallidos": self.fallidos,
                "indice": self.indice,
                "enlace": self.enlace,
                "archivo": self.archivo,
                "descargado": self.descargado,
                "total_bytes": self.total_bytes,
                "transcurrido": round(time.monotonic() - self.inicio, 1),
                "log": list(self._log),
                "resultados": list(self._resultados),
            }


class WebResolver(LinkResolver):
    """Resolver que envía su traza al log del trabajo en vez de a stderr."""

    def __init__(self, job: Job, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.job = job

    def _log(self, mensaje: str) -> None:
        self.job.anadir_log(f"{self.prefix}{mensaje}")


TRABAJOS: dict[str, Job] = {}
TRABAJOS_LOCK = threading.Lock()


def _limpiar_trabajos_bloqueado() -> None:
    ahora = time.monotonic()
    expirados = [
        trabajo_id
        for trabajo_id, trabajo in TRABAJOS.items()
        if trabajo.expirado(ahora)
    ]
    for trabajo_id in expirados:
        del TRABAJOS[trabajo_id]


def registrar_trabajo(trabajo: Job) -> None:
    with TRABAJOS_LOCK:
        _limpiar_trabajos_bloqueado()
        TRABAJOS[trabajo.id] = trabajo


def obtener_trabajo(trabajo_id: str) -> Job | None:
    with TRABAJOS_LOCK:
        return TRABAJOS.get(trabajo_id)


def _descargar_enlace(
    resolver: WebResolver,
    url: str,
    carpeta: Path,
    force: bool,
    browser: bool,
    on_progress: Callable[[int, int], None],
    opciones: dict[str, Any] | None = None,
) -> tuple[Path, str]:
    """Resuelve y descarga un enlace; devuelve (destino, nombre_de_archivo)."""
    if resolver.job.cancelar.is_set():
        raise ResolveError("Descarga cancelada por el usuario.")

    if browser:
        final_url, filename, destino = browser_resolve_and_download(
            url,
            str(carpeta),
            force=force,
            as_dir=True,
            timeout=resolver.timeout,
            max_wait=resolver.max_wait,
            verbose=True,
            user_agent=(opciones or {}).get("user_agent"),
            resolver=resolver,
        )
        # Mantener el nombre visible mientras se descarga por HTTP.
        resolver.job.registrar_archivo(filename)
        if destino is None:
            # Resolver por HTTP a partir de la URL final.
            resultado = Resolution(
                final_url=final_url,
                filename=filename,
                referer=url,
                hops=0,
                page_url=final_url,
            )
            destino = resolver.download(
                resultado,
                str(carpeta),
                force=force,
                as_dir=True,
                progress=on_progress,
            )
        return destino, filename

    resultado = resolver.resolve(url)
    resolver.job.registrar_archivo(resultado.filename)
    destino = resolver.download(
        resultado,
        str(carpeta),
        force=force,
        as_dir=True,
        progress=on_progress,
    )
    return destino, resultado.filename


def _obtener_enlace_final(
    resolver: WebResolver,
    url: str,
    browser: bool,
    opciones: dict[str, Any] | None = None,
) -> str:
    """Resuelve el enlace y devuelve sólo la URL final, sin guardar nada."""
    if resolver.job.cancelar.is_set():
        raise ResolveError("Resolución cancelada por el usuario.")
    if browser:
        final_url, _nombre, _destino = browser_resolve_and_download(
            url,
            None,
            timeout=resolver.timeout,
            max_wait=resolver.max_wait,
            verbose=True,
            save=False,
            user_agent=(opciones or {}).get("user_agent"),
            resolver=resolver,
        )
        return final_url
    resultado = resolver.resolve(url)
    if resultado.prefetched is not None:
        try:
            resultado.prefetched.unlink(missing_ok=True)
        except OSError:
            pass
    return resultado.final_url


def _resultado_ok(
    indice: int, url: str, destino: Path | None, inicio: float, final: str = ""
) -> Resultado:
    return {
        "indice": indice,
        "enlace": redact_url(url),
        "archivo": destino.name if destino is not None else "",
        "final": final,
        "estado": "ok",
        "error": "",
        "transcurrido": round(time.monotonic() - inicio, 1),
    }


def _registrar_fallo(
    job: Job,
    indice: int,
    url: str,
    exc: Exception,
    inicio: float,
) -> None:
    job.registrar_fallo()
    job.anadir_log(f"    error: {exc}")
    job.anadir_resultado(
        {
            "indice": indice,
            "enlace": redact_url(url),
            "archivo": "",
            "final": "",
            "estado": "error",
            "error": str(exc),
            "transcurrido": round(time.monotonic() - inicio, 1),
        }
    )


def _procesar_enlace(
    job: Job,
    indice: int,
    total: int,
    url: str,
    carpeta: Path,
    force: bool,
    browser: bool,
    auto_browser: bool,
    solo_enlace: bool,
    cache: ResolutionCache,
    log_lock: threading.Lock,
    opciones: dict[str, Any] | None = None,
) -> None:
    """Procesa un enlace, incluyendo el reintento opcional con navegador."""
    opciones = dict(opciones or {})
    opciones.setdefault("browser_fallback", auto_browser)
    job.iniciar_enlace(indice, url)
    job.anadir_log(f"[{indice}/{total}] {redact_url(url)}")
    inicio = time.monotonic()
    resolver = WebResolver(
        job,
        verbose=True,
        cache=cache,
        log_lock=log_lock,
        prefix=f"[{indice}/{total}] ",
        **opciones,
    )

    def on_progress(descargado: int, total_bytes: int) -> None:
        if job.cancelar.is_set():
            raise ResolveError("Descarga cancelada por el usuario.")
        job.actualizar_progreso(descargado, total_bytes)

    def intentar(usar_browser: bool) -> tuple[Path | None, str, str]:
        if solo_enlace:
            final = _obtener_enlace_final(resolver, url, usar_browser, opciones)
            return None, "", final
        destino, nombre = _descargar_enlace(
            resolver, url, carpeta, force, usar_browser, on_progress, opciones
        )
        return destino, nombre, ""

    try:
        destino, nombre, final = intentar(browser)
    except Exception as exc:
        if not (auto_browser and not browser):
            _registrar_fallo(job, indice, url, exc, inicio)
            return
        job.anadir_log(f"    HTTP falló ({exc}); reintentando con navegador...")
        try:
            destino, nombre, final = intentar(True)
        except Exception as exc2:
            _registrar_fallo(job, indice, url, exc2, inicio)
            return

    if solo_enlace:
        job.registrar_exito()
        job.anadir_log(f"    enlace final: {final}")
    else:
        job.registrar_archivo(nombre)
        job.registrar_exito()
        job.anadir_log(f"    guardado {destino.name if destino else nombre}")
    job.anadir_resultado(_resultado_ok(indice, url, destino, inicio, final))


def ejecutar_trabajo(
    job: Job,
    enlaces: list[str],
    carpeta: Path,
    force: bool,
    browser: bool = False,
    auto_browser: bool = False,
    solo_enlace: bool = False,
    opciones: dict[str, Any] | None = None,
) -> None:
    cache = ResolutionCache(CACHE_FILE, ttl=DEFAULT_CACHE_TTL, enabled=True)
    carpeta.mkdir(parents=True, exist_ok=True)
    log_lock = threading.Lock()
    total = len(enlaces)

    for indice, url in enumerate(enlaces, 1):
        if job.cancelar.is_set():
            break
        _procesar_enlace(
            job,
            indice,
            total,
            url,
            carpeta,
            force,
            browser,
            auto_browser,
            solo_enlace,
            cache,
            log_lock,
            opciones,
        )

    job.finalizar()
    completados, fallidos = job.resumen()
    accion = "resueltos" if solo_enlace else "descargados"
    job.anadir_log(
        f"Fin: {completados}/{job.total} {accion}, {fallidos} con error."
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "Desenlace/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(format, *args)

    def _solicitud_permitida(self) -> bool:
        hosts_permitidos = {host.lower() for host in ALLOWED_HOSTS}
        host_servidor = str(getattr(self.server, "server_address", ("", 0))[0]).lower()
        if host_servidor not in ("", "0.0.0.0", "::"):
            hosts_permitidos.add(host_servidor)

        host = self.headers.get("Host")
        if host:
            nombre_host = host.rsplit(":", 1)[0].strip("[]").lower()
            if nombre_host not in hosts_permitidos:
                return False

        origen = self.headers.get("Origin")
        if origen:
            try:
                nombre_origen = (urlsplit(origen).hostname or "").lower()
            except ValueError:
                nombre_origen = ""
            if nombre_origen and nombre_origen not in hosts_permitidos:
                return False
        return True

    def _responder(self, codigo: int, cuerpo: bytes, tipo: str) -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _json(self, datos: dict[str, Any], codigo: int = 200) -> None:
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self._responder(codigo, cuerpo, "application/json; charset=utf-8")

    def _leer_cuerpo(self) -> dict[str, Any]:
        longitud = int(self.headers.get("Content-Length") or 0)
        if longitud < 0 or longitud > MAX_REQUEST_BODY:
            raise ResolveError("Cuerpo de la petición demasiado grande o inválido.")
        if longitud <= 0:
            return {}
        try:
            datos = json.loads(self.rfile.read(longitud).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ResolveError(f"Cuerpo JSON no válido: {exc}") from exc
        if not isinstance(datos, dict):
            raise ResolveError("El cuerpo JSON debe ser un objeto.")
        return datos

    def do_GET(self) -> None:  # noqa: N802
        if not self._solicitud_permitida():
            self._json({"error": "Origen no permitido."}, 403)
            return
        ruta = self.path.split("?", 1)[0]
        if ruta in ("/", "/index.html"):
            try:
                cuerpo = HTML_FILE.read_bytes()
            except OSError:
                self._responder(500, b"Falta index.html", "text/plain; charset=utf-8")
                return
            self._responder(200, cuerpo, "text/html; charset=utf-8")
            return
        if ruta == "/api/estado":
            job = self._trabajo_de_query()
            if job is None:
                self._json({"error": "Trabajo no encontrado"}, 404)
                return
            self._json(job.instantanea())
            return
        self._json({"error": "Ruta no encontrada"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._solicitud_permitida():
            self._json({"error": "Origen no permitido."}, 403)
            return
        ruta = self.path.split("?", 1)[0]
        try:
            datos = self._leer_cuerpo()
        except ResolveError as exc:
            self._json({"error": str(exc)}, 400)
            return

        if ruta == "/api/carpeta":
            try:
                self._json({"carpeta": elegir_carpeta()})
            except ResolveError as exc:
                self._json({"error": str(exc)}, 500)
            return

        if ruta == "/api/descargar":
            self._descargar(datos)
            return

        if ruta == "/api/cancelar":
            job = obtener_trabajo(str(datos.get("id") or ""))
            if job is None:
                self._json({"error": "Trabajo no encontrado"}, 404)
                return
            job.cancelar.set()
            self._json({"ok": True})
            return

        self._json({"error": "Ruta no encontrada"}, 404)

    def _trabajo_de_query(self) -> Job | None:
        parametros = parse_qs(urlsplit(self.path).query)
        return obtener_trabajo((parametros.get("id") or [""])[0])

    def _descargar(self, datos: dict[str, Any]) -> None:
        try:
            enlaces = parse_links(str(datos.get("enlaces") or ""))
        except ResolveError as exc:
            self._json({"error": str(exc)}, 400)
            return

        solo_enlace = bool(datos.get("soloEnlace"))
        carpeta_texto = str(datos.get("carpeta") or "").strip()
        if not carpeta_texto and not solo_enlace:
            self._json({"error": "Selecciona una carpeta de destino."}, 400)
            return
        carpeta = Path(carpeta_texto or "downloads").expanduser()
        if not solo_enlace:
            try:
                carpeta.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._json({"error": f"No se puede usar esa carpeta: {exc}"}, 400)
                return
            if not carpeta.is_dir():
                self._json({"error": "La ruta de destino no es una carpeta."}, 400)
                return

        job = Job(len(enlaces))
        registrar_trabajo(job)
        hilo = threading.Thread(
            target=ejecutar_trabajo,
            args=(
                job,
                enlaces,
                carpeta,
                bool(datos.get("force")),
                bool(datos.get("browser")),
                bool(datos.get("autoBrowser")),
                solo_enlace,
                getattr(self.server, "resolver_options", {}),
            ),
            daemon=True,
        )
        hilo.start()
        self._json({"id": job.id, "total": len(enlaces)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Interfaz web local para descargar enlaces resueltos."
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Interfaz de escucha (por defecto 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Puerto (por defecto 8765)"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="No abrir el navegador al arrancar"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Registrar cada petición HTTP"
    )
    parser.add_argument(
        "--user-agent", metavar="UA", help="User-Agent para las peticiones"
    )
    parser.add_argument(
        "--cookies",
        metavar="ARCHIVO",
        help=(
            "Archivo de cookies (Netscape o 'nombre=valor') para sitios con "
            "Cloudflare o sesión iniciada"
        ),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        metavar="N",
        help=f"Reintentos ante errores de red o HTTP 429/5xx (por defecto {DEFAULT_RETRIES})",
    )
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        metavar="CABECERA",
        help="Cabecera HTTP extra. Se puede repetir.",
    )
    parser.add_argument(
        "--max-redirects",
        type=int,
        default=DEFAULT_MAX_REDIRECTS,
        metavar="N",
        help=(
            "Límite estricto de redirecciones HTTP "
            f"(por defecto {DEFAULT_MAX_REDIRECTS})"
        ),
    )
    parser.add_argument(
        "--allow-private",
        action="store_true",
        help=(
            "Permitir IPs privadas/loopback (desactiva SSRF). "
            "Sólo para pruebas locales"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not HTML_FILE.is_file():
        print(f"No se encuentra {HTML_FILE}", file=sys.stderr)
        return 1

    try:
        cookies = load_cookie_file(args.cookies) if args.cookies else None
        headers = parse_extra_headers(args.header)
    except ResolveError as exc:
        print(f"Error en las opciones: {exc}", file=sys.stderr)
        return 1

    servidor = ThreadingHTTPServer((args.host, args.port), Handler)
    servidor.daemon_threads = True
    servidor.allow_reuse_address = True
    servidor.verbose = args.verbose  # type: ignore[attr-defined]
    servidor.resolver_options = {  # type: ignore[attr-defined]
        "user_agent": args.user_agent,
        "headers": headers,
        "cookies": cookies,
        "retries": args.retries,
        "max_redirects": args.max_redirects,
        "allow_private": args.allow_private,
    }
    url = f"http://{args.host}:{args.port}/"
    print(f"Interfaz de descargas disponible en {url}")
    print("Pulsa Ctrl+C para detenerla.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenida.")
    finally:
        servidor.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
