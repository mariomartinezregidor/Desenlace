"""Diagnóstico de un enlace: explica en lenguaje llano qué ha pasado.

No descarga nada (equivale a ``--info``) y muestra cada paso del recorrido,
el módulo que reconoció cada página y un consejo si la resolución falla.
"""

from __future__ import annotations

import sys
import threading

from desenlace_core import redact_url
from orchestrator import Hooks, ProcessOptions, process_url


def _advice(message: str) -> list[str]:
    text = message.lower()
    tips: list[str] = []
    if "anti-bot" in text or "cloudflare" in text or "captcha" in text:
        tips.append(
            "La web muestra una verificación. Prueba con --auto-browser. Si "
            "sigue fallando, exporta tus cookies del navegador y usa --cookies."
        )
    if "no se reconoce" in text or "varios clics" in text:
        tips.append(
            "La página tiene un formato que no conocemos. Prueba con "
            "--auto-browser para que el navegador siga los pasos."
        )
    if "no pública" in text or "bloqueado" in text or "metadatos" in text:
        tips.append(
            "El enlace apunta a una dirección local o privada y se ha bloqueado "
            "por seguridad. Esto es lo esperado con enlaces normales."
        )
    if "telegram" in text:
        tips.append("Para bots de Telegram ejecuta: desenlace setup")
    if "límite" in text or "saltos" in text:
        tips.append("La cadena de redirecciones es muy larga. Prueba con --max-hops más alto.")
    if "timeout" in text or "tardó" in text or "agotó" in text:
        tips.append("La web tarda demasiado. Prueba con --max-wait o --timeout mayores.")
    return tips


def run_diagnose(url: str, options: ProcessOptions) -> int:
    options.info_only = True
    options.use_cache = False
    options.verbose = True
    lock = threading.Lock()

    def emit(line: str) -> None:
        with lock:
            print(line, file=sys.stderr, flush=True)

    trace: list[dict] = []
    result = process_url(url, options, Hooks(log=emit, log_lock=lock), trace=trace)

    print()
    print("DIAGNÓSTICO")
    print(f"Enlace: {redact_url(url)}")
    print()
    if trace:
        print("Recorrido:")
        for step in trace:
            action = step.get("action") or "ninguna (página final o no reconocida)"
            plugin = step.get("plugin")
            suffix = f" · módulo: {plugin}" if plugin else ""
            print(
                f"  {step['hop']:>2}. {step['method']} {step['url']} "
                f"-> HTTP {step['status']}"
            )
            print(f"      acción detectada: {action}{suffix}")
        print()
    else:
        print("No se ha seguido ningún paso.\n")

    if result.error is not None:
        message = str(result.error)
        print("Resultado: NO RESUELTO")
        print(f"Motivo: {message}")
        tips = _advice(message)
        if tips:
            print()
            print("Qué puedes hacer:")
            for tip in tips:
                print(f"  - {tip}")
        return 1

    final = result.resolution.final_url if result.resolution else "(desconocido)"
    print("Resultado: RESUELTO")
    print(f"Enlace final: {redact_url(final)}")
    if result.resolution:
        print(f"Nombre de archivo: {result.resolution.filename}")
    print()
    print("Nota: el diagnóstico no descarga el archivo. Quita --diagnose para descargarlo.")
    return 0
