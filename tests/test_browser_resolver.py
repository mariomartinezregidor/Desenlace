import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from browser_resolver import BrowserResolver, is_bot_challenge  # noqa: E402

CHALLENGE_HTML = (
    "<html><head><title>Just a moment...</title></head>"
    "<body><p>Checking your browser before accessing.</p>"
    '<div class="cf-turnstile"></div>'
    '<input type="checkbox" id="robot-check"></body></html>'
)
FINAL_HTML = "<html><head><title>Listo</title></head><body>Contenido</body></html>"


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def is_visible(self, timeout=0):
        return self.selector == "#robot-check" and not self.page.solved

    def click(self, timeout=0):
        if self.selector == "#robot-check":
            self.page.clicked = True
            self.page.solved = True
            self.page.url = self.page.final_url


class FakeFrameLocator:
    def locator(self, selector):
        return FakeLocator(self.page, f"frame:{selector}")


class FakePage:
    def __init__(self, start_url="https://example.com/challenge"):
        self.url = start_url
        self.final_url = "https://example.com/final"
        self.solved = False
        self.clicked = False
        self.events = {}

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    def title(self):
        return "Listo" if self.solved else "Just a moment..."

    def content(self):
        return FINAL_HTML if self.solved else CHALLENGE_HTML

    def locator(self, selector):
        return FakeLocator(self, selector)

    def frame_locator(self, selector):
        return FakeFrameLocator()

    def wait_for_load_state(self, state=None, timeout=None):
        return None

    def wait_for_timeout(self, ms):
        return None

    def on(self, event, callback):
        self.events[event] = callback


class FakeContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        self.page.on("download", lambda download: None)
        return self.page

    def cookies(self):
        return [
            {
                "name": "cf_clearance",
                "value": "token",
                "domain": ".example.com",
                "path": "/",
            }
        ]

    def add_cookies(self, cookies):
        self.added = cookies


class FakeBrowser:
    def __init__(self, page):
        self.page = page

    def new_context(self, **kwargs):
        return FakeContext(self.page)

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


class BrowserResolverTests(unittest.TestCase):
    def test_detects_bot_challenge_markers(self):
        self.assertTrue(is_bot_challenge(200, CHALLENGE_HTML))
        self.assertTrue(is_bot_challenge(403, "<h1>Attention Required! | Cloudflare</h1>"))
        self.assertFalse(is_bot_challenge(200, "<html><body>Artículo normal</body></html>"))

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
        class FinalPage(FakePage):
            def title(self):
                return "Listo"

            def content(self):
                return FINAL_HTML

        page = FinalPage()
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


if __name__ == "__main__":
    unittest.main()
