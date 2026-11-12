import base64
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import desenlace  # noqa: E402
from browser_resolver import BrowserOutcome  # noqa: E402
from desenlace import LinkResolver, Resolution  # noqa: E402

FILE_BYTES = b"%PDF-1.4 contenido de prueba\n"


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeShortener/1.0"

    def log_message(self, *args):
        pass

    def _html(self, body: str) -> None:
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/short":
            self._html(
                '<html><head><meta http-equiv="refresh" '
                'content="0; url=/step"></head><body></body></html>'
            )
            return
        if self.path == "/step":
            self._html(
                '<html><body>'
                '<form id="go-link" action="/links/go" method="post">'
                '<input type="hidden" name="_csrfToken" value="tok"/>'
                "</form>"
                '<script>var app_vars = {"counter_value":"0"};</script>'
                "</body></html>"
            )
            return
        if self.path.startswith("/go?"):
            from urllib.parse import parse_qs, urlsplit

            encoded = parse_qs(urlsplit(self.path).query)["url"][0]
            target = base64.urlsafe_b64decode(encoded + "==").decode()
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        if self.path == "/gate":
            self._html(
                "<html><body><p>Paso 1: haz clic aquí</p>"
                '<button id="step1">Clic aquí</button>'
                '<button id="download">Descargar</button>'
                "</body></html>"
            )
            return
        if self.path == "/file":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="doc"')
            self.send_header("Content-Length", str(len(FILE_BYTES)))
            self.end_headers()
            self.wfile.write(FILE_BYTES)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/links/go":
            payload = json.dumps(
                {"status": "success", "data": {"url": "/file"}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()


class ResolutionFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_meta_refresh_then_xhr_then_file(self):
        resolver = LinkResolver(timeout=5, max_wait=5, wait_buffer=0, allow_private=True)
        with redirect_stderr(io.StringIO()):
            result = resolver.resolve(f"{self.base}/short")
        self.assertEqual(result.final_url, f"{self.base}/file")
        self.assertEqual(result.filename, "doc.pdf")

    def test_gateway_redirect_with_base64(self):
        target = f"{self.base}/file"
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        resolver = LinkResolver(timeout=5, max_wait=5, wait_buffer=0, allow_private=True)
        with redirect_stderr(io.StringIO()):
            result = resolver.resolve(f"{self.base}/go?url={encoded}")
        self.assertEqual(result.final_url, target)

    def test_download_writes_content(self):
        resolver = LinkResolver(timeout=5, max_wait=5, wait_buffer=0, allow_private=True)
        with redirect_stderr(io.StringIO()):
            result = resolver.resolve(f"{self.base}/short")
            with tempfile.TemporaryDirectory() as directory:
                destination = resolver.download(result, directory, as_dir=True)
                self.assertTrue(destination.is_file())
                self.assertEqual(destination.read_bytes(), FILE_BYTES)
                self.assertEqual(destination.name, "doc.pdf")

    def test_multi_click_fallback_uses_headless_browser(self):
        base = self.base

        class FakeBrowserResolver:
            @staticmethod
            def available():
                return True

            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def resolve(self, url, **kwargs):
                return BrowserOutcome(
                    final_url=f"{base}/file",
                    extracted_url=f"{base}/file",
                    filename="doc.pdf",
                )

        resolver = LinkResolver(
            timeout=5,
            max_wait=5,
            retries=0,
            allow_private=True,
            browser_fallback=True,
        )
        with mock.patch.object(desenlace, "BrowserResolver", FakeBrowserResolver):
            with redirect_stderr(io.StringIO()):
                result = resolver.resolve(f"{base}/gate")
        self.assertEqual(result.final_url, f"{base}/file")

    def test_download_moves_prefetched_file(self):
        resolver = LinkResolver(timeout=5, max_wait=5, allow_private=True)
        with tempfile.TemporaryDirectory() as directory:
            prefetched = Path(directory) / "telegram-file.bin"
            prefetched.write_bytes(FILE_BYTES)
            result = Resolution(
                final_url="https://t.me/canal/1",
                filename="telegram-file.bin",
                referer="https://t.me/canal/1",
                hops=1,
                prefetched=prefetched,
            )
            output = Path(directory) / "salida"
            destination = resolver.download(result, str(output), as_dir=True)
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.read_bytes(), FILE_BYTES)
            self.assertFalse(prefetched.exists())


if __name__ == "__main__":
    unittest.main()
