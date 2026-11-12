"""Trabajos de descarga de la interfaz web.

Guarda el estado de cada petición (progreso, registro, resultados) y ejecuta
los enlaces con el orquestador común, igual que la línea de comandos.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, TypedDict

from desenlace import (
    CACHE_FILE,
    DEFAULT_CACHE_TTL,
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_RETRIES,
    Hooks,
    ProcessOptions,
    ResolutionCache,
    ResolveError,
    process_url,
    redact_url,
)

MAX_LOG_LINES = 400

JOB_RETENTION_SECONDS = 3600

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

def _opciones_web(
    carpeta: Path,
    force: bool,
    browser: bool,
    auto_browser: bool,
    solo_enlace: bool,
    cache: ResolutionCache,
    extra: dict[str, Any],
) -> ProcessOptions:
    """Traduce los ajustes de la interfaz al lenguaje del orquestador."""
    return ProcessOptions(
        output=str(carpeta),
        as_dir=True,
        force=force,
        info_only=solo_enlace,
        browser=browser,
        auto_browser=auto_browser,
        use_cache=True,
        verbose=True,
        cache=cache,
        user_agent=extra.get("user_agent"),
        headers=dict(extra.get("headers") or {}),
        cookies=extra.get("cookies"),
        retries=extra.get("retries", DEFAULT_RETRIES),
        max_redirects=extra.get("max_redirects", DEFAULT_MAX_REDIRECTS),
        allow_private=extra.get("allow_private", False),
        browser_timeout=extra.get("browser_timeout", 120.0),
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
    """Procesa un enlace con el orquestador común y registra el resultado."""
    job.iniciar_enlace(indice, url)
    job.anadir_log(f"[{indice}/{total}] {redact_url(url)}")
    inicio = time.monotonic()
    options = _opciones_web(
        carpeta, force, browser, auto_browser, solo_enlace, cache, opciones or {}
    )
    hooks = Hooks(
        log=job.anadir_log,
        progress=job.actualizar_progreso,
        cancelled=job.cancelar.is_set,
        on_filename=job.registrar_archivo,
        log_lock=log_lock,
    )
    resultado = process_url(url, options, hooks, index=indice, total=total)

    if resultado.error is not None:
        _registrar_fallo(job, indice, url, resultado.error, inicio)
        return

    if solo_enlace:
        final = resultado.resolution.final_url if resultado.resolution else ""
        job.registrar_exito()
        job.anadir_log(f"    enlace final: {final}")
        job.anadir_resultado(_resultado_ok(indice, url, None, inicio, final))
        return

    destino = resultado.destination
    if destino is None:
        _registrar_fallo(job, indice, url, ResolveError("No se obtuvo archivo."), inicio)
        return
    job.registrar_archivo(destino.name)
    job.registrar_exito()
    job.anadir_log(f"    guardado {destino.name}")
    job.anadir_resultado(_resultado_ok(indice, url, destino, inicio))

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


def preparar_destino(texto: str, solo_enlace: bool) -> Path:
    """Valida y crea la carpeta de destino. Lanza ResolveError si no es válida."""
    if not texto and not solo_enlace:
        raise ResolveError("Selecciona una carpeta de destino.")
    carpeta = Path(texto or "downloads").expanduser()
    if not solo_enlace:
        try:
            carpeta.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ResolveError(f"No se puede usar esa carpeta: {exc}") from exc
        if not carpeta.is_dir():
            raise ResolveError("La ruta de destino no es una carpeta.")
    return carpeta


def iniciar_trabajo(
    enlaces: list[str],
    carpeta: Path,
    *,
    force: bool,
    browser: bool,
    auto_browser: bool,
    solo_enlace: bool,
    opciones: dict[str, Any],
) -> Job:
    """Registra un trabajo nuevo y lo ejecuta en segundo plano."""
    job = Job(len(enlaces))
    registrar_trabajo(job)
    threading.Thread(
        target=ejecutar_trabajo,
        args=(job, enlaces, carpeta, force, browser, auto_browser, solo_enlace, opciones),
        daemon=True,
    ).start()
    return job

