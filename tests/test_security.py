import io
import sys
import threading
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desenlace import LinkResolver, ResolveError, UnsafeUrlError, validate_url  # noqa: E402


class SSRFValidationTests(unittest.TestCase):
    def test_blocks_non_http_schemes(self):
        for url in ("file:///etc/passwd", "ftp://example.com/file", "gopher://x"):
            with self.assertRaises(UnsafeUrlError):
                validate_url(url)

    def test_blocks_loopback_and_private_ranges(self):
        for url in (
            "http://127.0.0.1/",
            "http://127.0.0.1:8080/admin",
            "http://10.0.0.5/",
            "http://192.168.1.10/router",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://[fd00::1]/",
            "http://localhost/",
            "http://metadata.google.internal/computeMetadata/v1/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    validate_url(url)

    def test_blocks_embedded_credentials(self):
        with self.assertRaises(UnsafeUrlError):
            validate_url("http://user:pass@example.com/")

    def test_allows_public_ip_literal(self):
        validate_url("https://93.184.216.34/")
        validate_url("http://1.1.1.1/")

    def test_allow_private_escape_hatch(self):
        validate_url("http://127.0.0.1:8000/", allow_private=True)

    def test_resolver_blocks_private_by_default(self):
        resolver = LinkResolver(timeout=2, max_wait=2, retries=0)
        with self.assertRaises(UnsafeUrlError):
            resolver.resolve("http://127.0.0.1:9/")


class _RedirectHandler(BaseHTTPRequestHandler):
    """Servidor que redirige en bucle y otro que redirige una vez."""

    server_version = "SSRFTest/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
            return
        if self.path.startswith("/once"):
            self.send_response(302)
            self.send_header("Location", "/file")
            self.end_headers()
            return
        payload = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class RedirectLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_strict_redirect_limit(self):
        resolver = LinkResolver(
            timeout=5,
            max_wait=5,
            wait_buffer=0,
            retries=0,
            max_redirects=3,
            allow_private=True,
        )
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(ResolveError) as captured:
                resolver.resolve(f"{self.base}/loop")
        self.assertIn("3 redirecciones", str(captured.exception))

    def test_validates_every_redirect_hop(self):
        seen = []

        def recorder(url, *, allow_private=False):
            seen.append(url)

        resolver = LinkResolver(
            timeout=5,
            max_wait=5,
            wait_buffer=0,
            retries=0,
            allow_private=True,
        )
        with mock.patch("desenlace.validate_url", side_effect=recorder):
            with redirect_stderr(io.StringIO()):
                result = resolver.resolve(f"{self.base}/once")
        self.assertTrue(result.final_url.endswith("/file"))
        self.assertTrue(any(url.endswith("/once") for url in seen))
        self.assertTrue(any(url.endswith("/file") for url in seen))

    def test_redirect_to_private_is_blocked_without_escape_hatch(self):
        # El primer salto es público (127.0.0.1 permitido por el mock), pero
        # validate_url real bloquearía el destino privado.
        with self.assertRaises(UnsafeUrlError):
            validate_url(f"{self.base}/file")


class _PublicRedirectToPrivate(BaseHTTPRequestHandler):
    """Simula un host público que redirige a la red interna."""

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        self.send_response(302)
        self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
        self.end_headers()


class RedirectSSRFTests(unittest.TestCase):
    def test_redirect_to_metadata_endpoint_is_blocked(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _PublicRedirectToPrivate)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        resolver = LinkResolver(
            timeout=5,
            max_wait=5,
            retries=0,
            allow_private=True,
        )
        original = __import__("desenlace").validate_url

        def selective(url, *, allow_private=False):
            if "169.254.169.254" in url:
                original(url, allow_private=False)
            return None

        try:
            with mock.patch("desenlace.validate_url", side_effect=selective):
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(UnsafeUrlError):
                        resolver.resolve(f"http://{host}:{port}/")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
