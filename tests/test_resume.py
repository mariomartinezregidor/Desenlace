"""Reanudación de descargas con cabeceras Range y el fichero .part."""

from __future__ import annotations

import io
import os
import re
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from desenlace_core import ResolveError
from downloader import download_resolution
from models import Resolution
from safe_http import SafeSession

CONTENT = os.urandom(300_000)  # archivo binario "grande" de prueba
ETAG = '"v1-test"'
ETAG_V2 = '"v2-test"'


class _State:
    cut_next_bytes: int | None = None  # corta la siguiente respuesta completa
    cut_always: bool = False
    ignore_range: bool = False
    change_file: bool = False
    seen_ranges: list[str] = []
    seen_requests: int = 0
    no_validators: bool = False

    @classmethod
    def reset(cls):
        cls.cut_next_bytes = None
        cls.cut_always = False
        cls.ignore_range = False
        cls.change_file = False
        cls.seen_ranges = []
        cls.seen_requests = 0
        cls.no_validators = False


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        _State.seen_requests += 1
        if self.path != "/big.bin":
            self.send_response(404)
            self.end_headers()
            return
        body = CONTENT if not _State.change_file else CONTENT[::-1]
        current_etag = ETAG_V2 if _State.change_file else ETAG
        range_header = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        if range_header:
            _State.seen_ranges.append(range_header)
        start = 0
        status = 200
        # Como un servidor real: If-Range sólo admite el rango si el validador coincide.
        range_valid = if_range is None or if_range == current_etag
        if range_header and not _State.ignore_range and range_valid:
            match = re.match(r"bytes=(\d+)-", range_header)
            start = int(match.group(1)) if match else 0
            status = 206
        payload = body[start:]
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        if not _State.no_validators:
            self.send_header("ETag", current_etag)
        self.send_header("Content-Length", str(len(payload)))
        if status == 206:
            self.send_header(
                "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}"
            )
        self.end_headers()
        cut = None
        if _State.cut_next_bytes is not None and status == 200 and range_header is None:
            cut = _State.cut_next_bytes
            _State.cut_next_bytes = None
        if _State.cut_always and status == 200:
            cut = len(payload) // 2
        if cut is not None:
            self.wfile.write(payload[:cut])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(payload)


class ResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.url = f"http://{host}:{port}/big.bin"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        _State.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.session = SafeSession(allow_private=True)
        self.target = Resolution(
            final_url=self.url,
            filename="big.bin",
            referer=self.url,
            hops=1,
        )

    def tearDown(self):
        self.session.close()
        self.tmp.cleanup()

    def _download(self, **kwargs):
        with redirect_stderr(io.StringIO()):
            return download_resolution(
                self.session,
                self.target,
                str(self.out),
                timeout=5,
                as_dir=True,
                resume_retries=kwargs.pop("resume_retries", 2),
                **kwargs,
            )

    def test_cut_connection_is_resumed_automatically(self):
        _State.cut_next_bytes = 100_000
        destination = self._download()
        self.assertEqual(destination.read_bytes(), CONTENT)
        self.assertTrue(_State.seen_ranges, "debe haber pedido el resto con Range")
        # El cliente reanuda desde el último byte escrito a disco (bloque completo),
        # no desde el punto exacto del corte: es lo correcto, nunca se pierde ni duplica.
        offset = int(_State.seen_ranges[0].split("=")[1].rstrip("-"))
        self.assertTrue(0 < offset <= 100_000, _State.seen_ranges)
        self.assertFalse(Path(str(destination) + ".part").exists())
        self.assertFalse(Path(str(destination) + ".part.json").exists())

    def test_interrupted_download_keeps_progress_for_next_run(self):
        _State.cut_always = True
        with self.assertRaises(ResolveError) as captured:
            self._download(resume_retries=0)
        self.assertIn("vuelve a ejecutar", str(captured.exception))
        part = self.out / "big.bin.part"
        self.assertTrue(part.exists(), "el progreso debe conservarse")
        kept = part.stat().st_size
        self.assertGreater(kept, 0)

        # La siguiente ejecución (servidor sano) continúa desde ese punto.
        _State.reset()
        destination = self._download()
        self.assertEqual(destination.read_bytes(), CONTENT)
        self.assertEqual(_State.seen_ranges, [f"bytes={kept}-"])

    def test_server_ignoring_range_restarts_cleanly(self):
        _State.cut_next_bytes = 80_000
        _State.ignore_range = True
        destination = self._download()
        self.assertEqual(destination.read_bytes(), CONTENT)

    def test_changed_file_is_never_mixed(self):
        """Si el archivo cambió en el servidor, el validador no coincide y se reinicia."""
        _State.cut_always = True
        with self.assertRaises(ResolveError):
            self._download(resume_retries=0)
        kept = (self.out / "big.bin.part").stat().st_size
        self.assertGreater(kept, 0)

        _State.reset()
        _State.change_file = True  # el archivo cambió y, con él, su ETag
        destination = self._download()
        # If-Range no coincide: el servidor envía el archivo completo nuevo.
        self.assertEqual(destination.read_bytes(), CONTENT[::-1])
        # Sí intentó reanudar (Range enviado), pero el validador no coincidió,
        # así que el servidor devolvió el archivo completo: nada se mezcla.
        self.assertTrue(_State.seen_ranges, "debió intentar reanudar primero")

    def test_without_validators_no_resume_is_attempted(self):
        _State.no_validators = True
        _State.cut_always = True
        with self.assertRaises(ResolveError):
            self._download(resume_retries=0)
        self.assertTrue((self.out / "big.bin.part").exists())

        _State.reset()
        _State.no_validators = True
        destination = self._download()
        self.assertEqual(destination.read_bytes(), CONTENT)
        self.assertEqual(_State.seen_ranges, [], "sin validador nunca se envía Range")

    def test_force_ignores_partial_file(self):
        _State.cut_always = True
        with self.assertRaises(ResolveError):
            self._download(resume_retries=0)
        _State.reset()
        destination = self._download(force=True)
        self.assertEqual(destination.read_bytes(), CONTENT)
        self.assertEqual(_State.seen_ranges, [])


if __name__ == "__main__":
    unittest.main()
