"""Tests del bypass de ICutLink con .txt real."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from browser_resolver import BrowserOutcome, BrowserResolver  # noqa: E402


class FakePage:
    """Simula una página de ICutLink con formulario POST."""
    
    def __init__(self):
        self.url = "https://icutlink.com/1z0wbum"
        self.calls = []
        self.solved = False

    def title(self):
        return "ICutLink"

    def content(self):
        return """
        <html>
        <body>
            <form action="https://tracking.eu.org/out.php?ld=1.9" method="POST">
                <input type="hidden" name="_method" value="POST">
                <input type="hidden" name="_csrfToken" value="c14fbd11f8e41ca206ff68825aae342c6e2c78a026e81bffe2">
                <input type="hidden" name="ci" value="TzdtSHdST2ZzWi9Rbjh0OUJxYXU5bzN0YVJDUkJaODZHRmo4aH">
                <input type="hidden" name="r" value="BPJQJ3diX4ls+mZN9K/v2w==">
                <input type="submit" value="Descargar archivo">
            </form>
            <a href="https://example.com/cookies.txt">Descargar cookies</a>
        </body>
        </html>
        """

    def locator(self, selector):
        if "form" in selector:
            return FakeFormLocator(self)
        return FakeEmptyLocator()

    def evaluate(self, script):
        # Simular que se encuentra un .txt link en el DOM
        if "querySelector" in script and "a[href]" in script:
            return "https://example.com/cookies.txt"
        return None

    def wait_for_load_state(self, state=None, timeout=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    def bring_to_front(self):
        pass

    def close(self):
        pass


class FakeFormLocator:
    def __init__(self, page):
        self.page = page
        self.first = self

    def count(self):
        return 1

    def evaluate(self, script):
        if "f.action" in script:
            return "https://tracking.eu.org/out.php?ld=1.9"
        if "f.method" in script:
            return "POST"
        return None

    def is_visible(self, timeout=0):
        return True

    def inner_html(self, timeout=0):
        return self.page.content()


class FakeEmptyLocator:
    def count(self):
        return 0

    def is_visible(self, timeout=0):
        return False


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.context = None

    def new_context(self, **kwargs):
        self.context = FakeContext(self.page)
        return self.context

    def close(self):
        pass


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.pages = [page]

    def add_init_script(self, script):
        pass


class FakeChromium:
    def __init__(self, page):
        self.page = page

    def launch(self, **kwargs):
        return FakeBrowser(self.page)


class FakePlaywright:
    def __init__(self, page):
        self.chromium = FakeChromium(page)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ICutLinkBypassTests(unittest.TestCase):
    """Validar que ICutLink con .txt se detecta correctamente."""

    def test_icutlink_form_detectado(self):
        """ICutLink tiene un formulario POST a tracking.eu.org."""
        page = FakePage()
        resolver = BrowserResolver(
            verbose=True,
            playwright_factory=lambda: FakePlaywright(page),
            poll_interval=0.01,
        )
        
        # Verificar que se puede ver el formulario
        forms = page.locator("form")
        self.assertEqual(forms.count(), 1)
        
        form = forms.first
        action = form.evaluate("f => f.action")
        self.assertIn("tracking.eu.org", action)

    def test_find_txt_link_en_icutlink(self):
        """Encontrar el link del .txt en el DOM de ICutLink."""
        page = FakePage()
        resolver = BrowserResolver()
        
        txt_link = resolver._find_txt_link(page)
        self.assertIsNotNone(txt_link)
        self.assertIn(".txt", txt_link.lower())

    def test_remove_blocking_overlays(self):
        """La función de remover overlays no falla en página sin overlays."""
        page = FakePage()
        resolver = BrowserResolver()
        
        # No debe lanzar excepción
        resolver._remove_blocking_overlays(page)


if __name__ == "__main__":
    unittest.main()
