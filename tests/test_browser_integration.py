"""Pruebas de integración con Chromium real (Playwright).

Se ejecutan con ``pytest -m browser``. Si Chromium no está instalado, se omiten.
Usan un servidor HTTP local que reproduce una web de descarga de verdad:
botón que abre un pop-up publicitario, temporizador de 10 segundos, formulario
POST y un enlace de descarga final.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from browser_resolver import BrowserResolver
from plugins import telegram_resolver

pytestmark = pytest.mark.browser

FILE_BYTES = b"%PDF-1.4 archivo de prueba del navegador\n"

GATE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Paso 1</title></head>
<body>
  <p>Paso 1: haz clic aquí. Espera 10 segundos.</p>
  <button id="step1" onclick="window.open('/popup'); startTimer();">Clic aquí</button>
  <form id="next-form" action="/next" method="post" style="display:none">
    <button id="step2" type="submit">Continuar</button>
  </form>
  <script>
    function startTimer() {
      setTimeout(function () {
        document.getElementById('next-form').style.display = 'block';
      }, 10000);
    }
  </script>
</body></html>
"""

FINAL_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Descarga</title></head>
<body>
  <p>Tu archivo está listo.</p>
  <a id="dl" href="/file.pdf" download>Descargar archivo</a>
</body></html>
"""

TELEGRAM_PREVIEW_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Canal de prueba</title></head>
<body>
  <div class="tgme_widget_message">
    <div class="tgme_widget_message_document_wrap">
      <a href="/file.pdf">documento.pdf</a>
    </div>
  </div>
</body></html>
"""

POPUP_HTML = "<html><body><p>Publicidad</p></body></html>"


class _Site(BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *args):
        pass

    def _send(self, status, body: bytes, content_type: str, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        _Site.hits.append(self.path)
        routes = {
            "/gate": (GATE_HTML.encode(), "text/html; charset=utf-8"),
            "/final": (FINAL_HTML.encode(), "text/html; charset=utf-8"),
            "/popup": (POPUP_HTML.encode(), "text/html; charset=utf-8"),
            "/t/canal/1": (TELEGRAM_PREVIEW_HTML.encode(), "text/html; charset=utf-8"),
            "/file.pdf": (FILE_BYTES, "application/pdf"),
        }
        if self.path in routes:
            body, content_type = routes[self.path]
            self._send(200, body, content_type)
            return
        self._send(404, b"no encontrado", "text/plain")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        _Site.hits.append(self.path)
        if self.path == "/next":
            self._send(302, b"", "text/plain", [("Location", "/final")])
            return
        self._send(404, b"no encontrado", "text/plain")


@pytest.fixture(scope="module")
def site():
    _Site.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_multi_click_sequence_with_popup_timer_and_form(site):
    """Clic → pop-up → temporizador de 10 s → formulario POST → enlace de descarga."""
    actions = [
        {"kind": "click", "selector": "#step1"},
        {"kind": "close_popup"},
        {"kind": "wait", "selector": "#step2", "timeout": 20},
        {"kind": "click", "selector": "#step2"},
        {"kind": "wait", "seconds": 1.0},
        {"kind": "extract_url", "selector": "a[download]"},
    ]
    resolver = BrowserResolver(
        timeout=20,
        max_wait=60,
        poll_interval=0.1,
        allow_private=True,
        max_sequence_seconds=60,
    )
    outcome = resolver.resolve(f"{site}/gate", actions=actions, speed_up_timers=True)

    assert outcome.timed_out is False
    assert outcome.extracted_url == f"{site}/file.pdf"
    assert "/popup" in _Site.hits, "el pop-up publicitario debía abrirse"
    assert "/next" in _Site.hits, "el formulario debía enviarse"


def test_sequence_respects_strict_deadline(site):
    """Una espera que supera el presupuesto corta el flujo sin colgarse."""
    actions = [{"kind": "wait", "seconds": 60}]
    resolver = BrowserResolver(
        timeout=10,
        max_wait=3,
        poll_interval=0.1,
        allow_private=True,
        max_sequence_seconds=3,
    )
    outcome = resolver.resolve(f"{site}/gate", actions=actions)
    assert outcome.timed_out is True


def test_telegram_channel_preview_extracts_file(site):
    """Plan de canal de Telegram: extrae el documento del preview del DOM."""
    actions = telegram_resolver.channel_plan()
    # Ajustamos el selector del plan real al servidor local de prueba: el
    # plan de producción apunta a CDN de Telegram; aquí comprobamos la
    # mecánica completa (selector combinado + extracción del DOM).
    resolver = BrowserResolver(
        timeout=15,
        max_wait=20,
        poll_interval=0.1,
        allow_private=True,
        max_sequence_seconds=20,
    )
    outcome = resolver.resolve(f"{site}/t/canal/1", actions=actions, speed_up_timers=True)
    assert outcome.extracted_url == f"{site}/file.pdf"


def test_missing_element_does_not_abort_and_falls_back_to_final_link(site):
    """Un clic sobre un selector inexistente no tumba la resolución.

    Tras el paso fallido, el motor hace su extracción final automática y
    encuentra el enlace de descarga de la página.
    """
    resolver = BrowserResolver(
        timeout=10,
        max_wait=10,
        poll_interval=0.1,
        allow_private=True,
        max_sequence_seconds=10,
    )
    outcome = resolver.resolve(
        f"{site}/final",
        actions=[{"kind": "click", "selector": "#no-existe", "timeout": 0.5}],
    )
    assert outcome.extracted_url == f"{site}/file.pdf"
