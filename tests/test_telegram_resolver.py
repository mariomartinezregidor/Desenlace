import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from browser_resolver import BrowserResolver  # noqa: E402
from plugins import telegram_resolver as tg  # noqa: E402
import telegram_client  # noqa: E402
from telegram_client import (  # noqa: E402
    TelegramConfigError,
    parse_bot_link,
    resolve_bot_link,
)

TELEGRAM_FILE = "https://cdn4.telegram-cdn.org/file/abc123/documento.pdf"


class _Locator:
    @property
    def first(self):
        return self

    def is_visible(self, timeout=0):
        return False

    def click(self, timeout=0):
        return None


class _Page:
    def __init__(self, url):
        self.url = url
        self.init_scripts = []

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    def title(self):
        return "Telegram"

    def content(self):
        return "<html><body>Telegram preview</body></html>"

    def locator(self, selector):
        return _Locator()

    def get_by_text(self, text, exact=False):
        return _Locator()

    def frame_locator(self, selector):
        return self

    def wait_for_load_state(self, state=None, timeout=None):
        return None

    def wait_for_timeout(self, ms):
        time.sleep(min(ms / 1000.0, 0.05))

    def wait_for_selector(self, selector, timeout=None):
        return True

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def bring_to_front(self):
        return None

    def close(self):
        return None

    def on(self, event, callback):
        return None

    def evaluate(self, script, arg=None):
        if "querySelectorAll(selector)" in script:
            return {"url": TELEGRAM_FILE, "form": False}
        return None


class _Context:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page

    def on(self, event, callback):
        return None

    def cookies(self):
        return []

    def add_cookies(self, cookies):
        return None


class _Browser:
    def __init__(self, page):
        self.page = page

    def new_context(self, **kwargs):
        return _Context(self.page)

    def close(self):
        return None


class _Chromium:
    def __init__(self, page):
        self.page = page

    def launch(self, headless=True):
        return _Browser(self.page)


class _Playwright:
    def __init__(self, page):
        self.chromium = _Chromium(page)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TelegramPluginTests(unittest.TestCase):
    def test_parse_channel_and_bot_links(self):
        channel = tg.parse_telegram_link("https://t.me/micanal/123")
        self.assertEqual(
            channel,
            {"kind": "channel", "channel": "micanal", "message_id": "123"},
        )
        bot = tg.parse_telegram_link("https://t.me/MiBot?start=abc")
        self.assertEqual(bot, {"kind": "bot", "bot": "MiBot", "start": "abc"})
        self.assertIsNone(tg.parse_telegram_link("https://example.com/canal/1"))

    def test_channel_action_uses_playwright_preview(self):
        action = tg.detect_url("https://t.me/micanal/123", ("t.me",))
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "browser")
        self.assertEqual(action.url, "https://t.me/s/micanal/123")
        self.assertTrue(action.plan)
        self.assertIn("extract_url", [step["kind"] for step in action.plan])

    def test_bot_action(self):
        action = tg.detect_url("https://t.me/MiBot?start=payload", ("t.me",))
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "telegram_bot")
        self.assertEqual(action.filename, "payload")

    def test_mocked_playwright_extracts_channel_file(self):
        page = _Page("https://t.me/s/micanal/123")
        resolver = BrowserResolver(
            timeout=5,
            max_wait=3,
            playwright_factory=lambda: _Playwright(page),
            poll_interval=0.01,
            allow_private=True,
        )
        outcome = resolver.resolve(
            "https://t.me/s/micanal/123",
            actions=tg.channel_plan(),
            speed_up_timers=True,
        )
        self.assertEqual(outcome.extracted_url, TELEGRAM_FILE)
        self.assertEqual(outcome.final_url, TELEGRAM_FILE)

    def test_bot_without_credentials_raises_controlled_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(telegram_client, "_load_dotenv", lambda: None):
                with self.assertRaises(TelegramConfigError):
                    resolve_bot_link("https://t.me/MiBot?start=abc")

    def test_bot_link_parser(self):
        self.assertEqual(
            parse_bot_link("https://t.me/MiBot?start=abc"),
            ("MiBot", "/start abc"),
        )
        self.assertEqual(parse_bot_link("https://t.me/MiBot"), ("MiBot", "/start"))

    def test_load_dotenv(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                "# comentario\nTELEGRAM_API_ID=123\n"
                "TELEGRAM_API_HASH='abcdef'\nTELEGRAM_SESSION_STRING=\"xyz\"\n"
            )
            path = Path(handle.name)
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                telegram_client._load_dotenv(path)
                self.assertEqual(os.environ["TELEGRAM_API_ID"], "123")
                self.assertEqual(os.environ["TELEGRAM_API_HASH"], "abcdef")
                self.assertEqual(os.environ["TELEGRAM_SESSION_STRING"], "xyz")
                self.assertTrue(telegram_client.telegram_configured())
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
