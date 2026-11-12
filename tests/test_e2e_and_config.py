"""Pruebas de extremo a extremo (CLI y web) y de configuración/secretos."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import desenlace_web  # noqa: E402
import web_jobs  # noqa: E402
from downloader import download_resolution  # noqa: E402
from models import Resolution  # noqa: E402
from safe_http import SafeSession  # noqa: E402

FILE_BYTES = b"contenido de prueba e2e\n"


class _LocalSite(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/enlace":
            body, ctype = b'<html><head><meta http-equiv="refresh" content="0; url=/archivo"></head></html>', "text/html"
        elif self.path == "/archivo":
            body, ctype = FILE_BYTES, "application/octet-stream"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, thread, f"http://{host}:{port}"


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _run_cli(*args, env=None):
    merged = dict(os.environ)
    merged.pop("DESENLACE_ALLOW_PRIVATE", None)
    merged.update(env or {})
    return subprocess.run(
        [sys.executable, str(ROOT / "desenlace.py"), *args],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=ROOT,
        env=merged,
    )


class CliEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.thread, cls.base = _start(_LocalSite)

    @classmethod
    def tearDownClass(cls):
        _stop(cls.server, cls.thread)

    def test_info_resolves_meta_refresh_with_allow_private(self):
        result = _run_cli("--info", "--no-cache", "--allow-private", f"{self.base}/enlace")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"{self.base}/archivo")

    def test_blocks_local_address_by_default(self):
        result = _run_cli("--info", "--no-cache", f"{self.base}/enlace")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no pública", result.stderr + result.stdout)

    def test_download_writes_file_to_output(self):
        with tempfile.TemporaryDirectory() as directory:
            result = _run_cli(
                "--no-cache", "--allow-private", "-o", directory, f"{self.base}/enlace"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            files = list(Path(directory).iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), FILE_BYTES)

    def test_diagnose_explains_the_chain(self):
        result = _run_cli("--diagnose", "--no-cache", "--allow-private", f"{self.base}/enlace")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DIAGNÓSTICO", result.stdout)
        self.assertIn("RESUELTO", result.stdout)
        self.assertIn("Recorrido:", result.stdout)

    def test_diagnose_reports_failure_with_advice(self):
        result = _run_cli("--diagnose", "--no-cache", f"{self.base}/enlace")
        self.assertEqual(result.returncode, 1)
        self.assertIn("NO RESUELTO", result.stdout)
        self.assertIn("Motivo:", result.stdout)

    def test_allow_private_env_prints_warning(self):
        result = _run_cli(
            "--info", "--no-cache", f"{self.base}/enlace",
            env={"DESENLACE_ALLOW_PRIVATE": "1"},
        )
        self.assertIn("AVISO: --allow-private activo", result.stderr)

    def test_invalid_setting_is_reported_cleanly(self):
        result = _run_cli("--info", "x", env={"DESENLACE_MAX_REDIRECTS": "abc"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("DESENLACE_MAX_REDIRECTS", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class WebApiEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site, cls.site_thread, cls.base = _start(_LocalSite)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cache_patch = mock.patch.object(
            web_jobs, "CACHE_FILE", Path(cls.tmp.name) / "cache.json"
        )
        cls.cache_patch.start()
        cls.api, cls.api_thread = cls._start_api()

    @classmethod
    def tearDownClass(cls):
        cls.cache_patch.stop()
        _stop(cls.api, cls.api_thread)
        _stop(cls.site, cls.site_thread)
        cls.tmp.cleanup()

    @classmethod
    def _start_api(cls):
        server = ThreadingHTTPServer(("127.0.0.1", 0), desenlace_web.Handler)
        server.verbose = False
        server.resolver_options = {
            "user_agent": None,
            "headers": {},
            "cookies": None,
            "retries": 0,
            "max_redirects": 10,
            "allow_private": True,
            "browser_timeout": 60.0,
        }
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        cls.api_base = f"http://{host}:{port}"
        return server, thread

    def _post(self, path, payload):
        request = Request(
            self.api_base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def _get(self, path):
        with urlopen(self.api_base + path, timeout=30) as response:
            return json.loads(response.read())

    def test_solo_enlace_job_returns_final_url(self):
        started = self._post(
            "/api/descargar",
            {"enlaces": f"{self.base}/enlace", "soloEnlace": True},
        )
        deadline = time.monotonic() + 60
        state = {}
        while time.monotonic() < deadline:
            state = self._get(f"/api/estado?id={started['id']}")
            if state["estado"] != "en_curso":
                break
            time.sleep(0.2)
        self.assertEqual(state["estado"], "terminado", state.get("log"))
        finales = [r["final"] for r in state["resultados"] if r["estado"] == "ok"]
        self.assertEqual(finales, [f"{self.base}/archivo"])

    def test_rejects_invalid_links_with_400(self):
        request = Request(
            self.api_base + "/api/descargar",
            data=json.dumps({"enlaces": "no-es-una-url"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        from urllib.error import HTTPError

        with self.assertRaises(HTTPError) as captured:
            urlopen(request, timeout=10)
        self.assertEqual(captured.exception.code, 400)


class ConfigAndSecretsTests(unittest.TestCase):
    def test_keyring_is_preferred_over_environment(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_API_HASH": "valor-entorno"}), mock.patch(
            "config._keyring_get", return_value="valor-llavero"
        ):
            self.assertEqual(config.get_secret("TELEGRAM_API_HASH"), "valor-llavero")
            self.assertEqual(config.secret_source("TELEGRAM_API_HASH"), "llavero")

    def test_environment_used_when_keyring_empty(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_API_HASH": "valor-entorno"}), mock.patch(
            "config._keyring_get", return_value=None
        ):
            self.assertEqual(config.get_secret("TELEGRAM_API_HASH"), "valor-entorno")

    def test_store_secret_refuses_unknown_names(self):
        with self.assertRaises(config.ConfigError):
            config.store_secret("OTRA_VARIABLE", "x")

    def test_settings_from_environment(self):
        with mock.patch.dict(
            os.environ,
            {
                "DESENLACE_MAX_REDIRECTS": "4",
                "DESENLACE_BROWSER_TIMEOUT": "45",
                "DESENLACE_ALLOW_PRIVATE": "0",
            },
        ):
            settings = config.load_settings(dotenv={})
        self.assertEqual(settings.max_redirects, 4)
        self.assertEqual(settings.browser_timeout, 45.0)
        self.assertFalse(settings.allow_private)

    def test_settings_validation(self):
        with mock.patch.dict(os.environ, {"DESENLACE_MAX_REDIRECTS": "0"}):
            with self.assertRaises(config.ConfigError):
                config.load_settings(dotenv={})
        with mock.patch.dict(os.environ, {"DESENLACE_ALLOW_PRIVATE": "quizas"}):
            with self.assertRaises(config.ConfigError):
                config.load_settings(dotenv={})

    def test_dotenv_values_used_for_settings(self):
        settings = config.load_settings(dotenv={"DESENLACE_MAX_REDIRECTS": "6"})
        self.assertEqual(settings.max_redirects, 6)

    def test_setup_never_prints_secret_values(self):
        import setup_wizard

        secret = "SECRETO-NO-DEBE-APARECER-123"
        out = StringIO()
        with mock.patch.dict(os.environ, {"TELEGRAM_API_HASH": secret}), mock.patch(
            "config._keyring_get", return_value=None
        ), mock.patch("sys.stdout", out):
            setup_wizard._telegram(assume_yes=True, interactive=False)
        self.assertNotIn(secret, out.getvalue())
        self.assertIn("configurado", out.getvalue())


class TemporaryFileCleanupTests(unittest.TestCase):
    def test_prefetched_file_moves_to_destination_and_cleans_temp_dir(self):
        with tempfile.TemporaryDirectory() as base:
            temp_dir = Path(base) / "desenlace-telegram-prueba"
            temp_dir.mkdir()
            source = temp_dir / "documento.bin"
            source.write_bytes(FILE_BYTES)
            target = Resolution(
                final_url="https://t.me/canal/1",
                filename="documento.bin",
                referer="https://t.me/canal/1",
                hops=1,
                prefetched=source,
            )
            output = Path(base) / "salida"
            destination = download_resolution(
                SafeSession(allow_private=True), target, str(output),
                timeout=5, as_dir=True,
            )
            self.assertEqual(destination.read_bytes(), FILE_BYTES)
            self.assertFalse(temp_dir.exists(), "la carpeta temporal debe borrarse")

    def test_discard_never_touches_foreign_directories(self):
        from desenlace_core import discard_temp_file

        with tempfile.TemporaryDirectory() as base:
            foreign = Path(base) / "carpeta-del-usuario"
            foreign.mkdir()
            file_path = foreign / "archivo.bin"
            file_path.write_bytes(b"x")
            discard_temp_file(file_path)
            self.assertFalse(file_path.exists())
            self.assertTrue(foreign.exists(), "no debe borrar carpetas ajenas")


if __name__ == "__main__":
    unittest.main()
