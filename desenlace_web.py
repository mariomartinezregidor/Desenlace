#!/usr/bin/env python3
"""Desenlace: interfaz web local.

Sirve la página en 127.0.0.1 y expone una pequeña API JSON. La lógica de
descarga está en web_jobs.py y orchestrator.py.

Uso:
    desenlace-web
    desenlace-web --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import config
from desenlace import DEFAULT_MAX_REDIRECTS, DEFAULT_RETRIES, ResolveError, load_cookie_file, parse_extra_headers
from web_jobs import Job, iniciar_trabajo, obtener_trabajo, parse_links, preparar_destino

# La interfaz viaja dentro del paquete (carpeta webui/).
BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "webui" / "index.html"

MAX_REQUEST_BODY = 1_000_000
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

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
        if ruta == "/api/eventos":
            job = self._trabajo_de_query()
            if job is None:
                self._json({"error": "Trabajo no encontrado"}, 404)
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                while True:
                    datos = job.instantanea()
                    cuerpo = ("data: " + json.dumps(datos, ensure_ascii=False) + "\n\n").encode("utf-8")
                    self.wfile.write(cuerpo)
                    self.wfile.flush()
                    if datos.get("estado") != "en_curso":
                        break
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
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
            solo_enlace = bool(datos.get("soloEnlace"))
            carpeta = preparar_destino(str(datos.get("carpeta") or "").strip(), solo_enlace)
        except ResolveError as exc:
            self._json({"error": str(exc)}, 400)
            return

        job = iniciar_trabajo(
            enlaces,
            carpeta,
            force=bool(datos.get("force")),
            browser=bool(datos.get("browser")),
            auto_browser=bool(datos.get("autoBrowser")),
            solo_enlace=solo_enlace,
            opciones=getattr(self.server, "resolver_options", {}),
        )
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
        default=None,
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
        settings = config.load_settings()
    except (ResolveError, config.ConfigError) as exc:
        print(f"Error en las opciones: {exc}", file=sys.stderr)
        return 1

    max_redirects = args.max_redirects if args.max_redirects is not None else settings.max_redirects
    allow_private = bool(args.allow_private or settings.allow_private)
    if allow_private:
        print(
            "AVISO: --allow-private activo. La protección contra redes locales "
            "está desactivada (los endpoints de metadatos siguen bloqueados).",
            file=sys.stderr,
        )

    servidor = ThreadingHTTPServer((args.host, args.port), Handler)
    servidor.daemon_threads = True
    servidor.allow_reuse_address = True
    servidor.verbose = args.verbose  # type: ignore[attr-defined]
    servidor.resolver_options = {  # type: ignore[attr-defined]
        "user_agent": args.user_agent,
        "headers": headers,
        "cookies": cookies,
        "retries": args.retries,
        "max_redirects": max_redirects,
        "allow_private": allow_private,
        "browser_timeout": settings.browser_timeout,
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
