import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from browser_resolver import (  # noqa: E402
    TIMER_EVASION_SCRIPT,
    BrowserResolver,
    is_bot_challenge,
)

CHALLENGE_HTML = (
    "<html><head><title>Just a moment...</title></head>"
    "<body><p>Checking your browser before accessing.</p>"
    '<div class="cf-turnstile"></div>'
    '<input type="checkbox" id="robot-check"></body></html>'
)
FINAL_HTML = "<html><head><title>Listo</title></head><body>Contenido</body></html>"
DOWNLOAD_URL = "https://example.com/archivo.zip"


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def is_visible(self, timeout=0):
        if self.selector == "#robot-check":
            return not self.page.solved
        return self.selector in {"#step1", "#download", "text:Descargar"}

    def click(self, timeout=0):
        self.page.clicked.append(self.selector)
        if self.selector == "#step1" and self.page.context is not None:
            self.page.context.open_popup("https://ads.example/popup")
        if self.selector in {"#download", "#step1"}:
            self.page.ready = True
        if self.selector == "#robot-check":
            self.page.solved = True
            self.page.url = self.page.final_url


class FakeFrameLocator:
    def __init__(self, page):
        self.page = page

    def locator(self, selector):
        return FakeLocator(self.page, f"frame:{selector}")


class FakePage:
    def __init__(self, url="https://example.com/challenge", context=None):
        self.url = url
        self.final_url = "https://example.com/final"
        self.solved = False
        self.ready = False
        self.context = context
        self.closed = False
        self.clicked: list[str] = []
        self.events = {}
        self.init_scripts: list[str] = []
        self.content_value = CHALLENGE_HTML
        self.title_value = "Just a moment..."

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    def title(self):
        return "Listo" if self.solved else self.title_value

    def content(self):
        return FINAL_HTML if self.solved else self.content_value

    def locator(self, selector):
        return FakeLocator(self, selector)

    def get_by_text(self, text, exact=False):
        return FakeLocator(self, f"text:{text}")

    def frame_locator(self, selector):
        return FakeFrameLocator(self)

    def wait_for_load_state(self, state=None, timeout=None):
        return None

    def wait_for_timeout(self, ms):
        time.sleep(min(ms / 1000.0, 0.5))

    def wait_for_selector(self, selector, timeout=None):
        return True

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def bring_to_front(self):
        return None

    def close(self):
        self.closed = True

    def on(self, event, callback):
        self.events[event] = callback

    def evaluate(self, script, arg=None):
        if "querySelectorAll(selector)" in script:
            return {"url": DOWNLOAD_URL if self.ready else "", "form": False}
        return None


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.page.context = self
        self.page_handlers = []
        self.popups: list[FakePage] = []
        self.cookies_list = [
            {
                "name": "cf_clearance",
                "value": "token",
                "domain": ".example.com",
                "path": "/",
            }
        ]

    def new_page(self):
        return self.page

    def on(self, event, callback):
        if event == "page":
            self.page_handlers.append(callback)

    def open_popup(self, url):
        popup = FakePage(url=url)
        self.popups.append(popup)
        for handler in self.page_handlers:
            handler(popup)
        return popup

    def cookies(self):
        return self.cookies_list

    def add_cookies(self, cookies):
        self.added = cookies


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.context = None

    def new_context(self, **kwargs):
        self.context = FakeContext(self.page)
        return self.context

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, page):
        self.page = page

    def launch(self, headless=True):
        self.headless = headless
        return FakeBrowser(self.page)


class FakePlaywright:
    def __init__(self, page):
        self.chromium = FakeChromium(page)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeRoute:
    def __init__(self, url):
        self.request = type("Request", (), {"url": url})()
        self.aborted = False
        self.continued = False

    def abort(self):
        self.aborted = True

    def continue_(self):
        self.continued = True


class BrowserResolverTests(unittest.TestCase):
    def test_browser_guard_blocks_private_networks(self):
        resolver = BrowserResolver(allow_private=False)
        blocked = FakeRoute("http://169.254.169.254/latest/meta-data/")
        resolver._guard_route(blocked)
        self.assertTrue(blocked.aborted)
        self.assertFalse(blocked.continued)

        allowed = FakeRoute("https://93.184.216.34/file.zip")
        resolver._guard_route(allowed)
        self.assertTrue(allowed.continued)
        self.assertFalse(allowed.aborted)

        local = FakeRoute("http://127.0.0.1:8000/f")
        BrowserResolver(allow_private=True)._guard_route(local)
        self.assertTrue(local.continued)

    def test_detects_bot_challenge_markers(self):
        self.assertTrue(is_bot_challenge(200, CHALLENGE_HTML))
        self.assertTrue(
            is_bot_challenge(403, "<h1>Attention Required! | Cloudflare</h1>")
        )
        self.assertFalse(
            is_bot_challenge(200, "<html><body>Artículo normal</body></html>")
        )

    def test_mocked_headless_clicks_robot_checkbox(self):
        page = FakePage()
        resolver = BrowserResolver(
            timeout=5,
            max_wait=2,
            verbose=False,
            playwright_factory=lambda: FakePlaywright(page),
            poll_interval=0.01,
        )
        outcome = resolver.resolve("https://example.com/challenge")

        self.assertTrue(page.clicked, "debería haber pulsado el checkbox del captcha")
        self.assertEqual(outcome.final_url, "https://example.com/final")
        self.assertEqual(outcome.filename, "final")
        self.assertIn("cf_clearance", [cookie["name"] for cookie in outcome.cookies])
        self.assertIn("Contenido", outcome.html)

    def test_no_challenge_does_not_click(self):
        page = FakePage(url="https://example.com/final")
        page.solved = True
        resolver = BrowserResolver(
            timeout=5,
            max_wait=1,
            playwright_factory=lambda: FakePlaywright(page),
            poll_interval=0.01,
        )
        outcome = resolver.resolve("https://example.com/final")
        self.assertFalse(page.clicked)
        self.assertEqual(outcome.final_url, "https://example.com/final")

    def test_multi_click_closes_popup_and_extracts_url(self):
        page = FakePage(url="https://download.example/paso-1")
        page.solved = True
        page.title_value = "Descarga"
        page.content_value = "<html><body>Paso 1</body></html>"
        resolver = BrowserResolver(
            timeout=5,
            max_wait=10,
            playwright_factory=lambda: FakePlaywright(page),
            poll_interval=0.01,
            allow_private=True,
        )
        plan = [
            {"kind": "click", "selector": "#step1"},
            {"kind": "close_popup"},
            {"kind": "click", "selector": "#download"},
            {"kind": "extract_url"},
        ]
        outcome = resolver.resolve(
            "https://download.example/paso-1",
            actions=plan,
            speed_up_timers=True,
        )

        self.assertIn("#step1", page.clicked)
        self.assertIn("#download", page.clicked)
        self.assertEqual(outcome.extracted_url, DOWNLOAD_URL)
        self.assertEqual(outcome.final_url, DOWNLOAD_URL)
        self.assertFalse(outcome.timed_out)
        self.assertTrue(page.context.page_handlers, "debe escuchar nuevas pestañas")
        # El popup creado al pulsar #step1 debe haberse cerrado.
        self.assertTrue(page.context.popups)
        self.assertTrue(page.context.popups[0].closed)
        self.assertIn(TIMER_EVASION_SCRIPT, page.init_scripts)

    def test_multi_click_sequence_timeout_is_strict(self):
        page = FakePage(url="https://download.example/lento")
        page.solved = True
        resolver = BrowserResolver(
            timeout=1,
            max_wait=0.2,
            playwright_factory=lambda: FakePlaywright(page),
            poll_interval=0.01,
            max_sequence_seconds=0.2,
        )
        outcome = resolver.resolve(
            "https://download.example/lento",
            actions=[{"kind": "wait", "seconds": 30}],
        )
        self.assertTrue(outcome.timed_out)


if __name__ == "__main__":
    unittest.main()
