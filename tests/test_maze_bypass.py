"""Pruebas del bypass extremo: popups y botones por texto."""

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from browser_resolver import BrowserOutcome, BrowserResolver  # noqa: E402

FINAL_TXT = "https://cdn.example.com/cookies/lista.txt"


class FakeButton:
    def __init__(self, page, texto="Descargar archivo"):
        self.page = page
        self.texto = texto
        self.clicks = 0

    def is_visible(self, timeout=0):
        return True

    def inner_text(self):
        return self.texto

    def get_attribute(self, name):
        return None

    def click(self, timeout=0, force=False):
        self.clicks += 1
        self.page.url = FINAL_TXT

    def evaluate(self, script):
        self.page.url = FINAL_TXT


class EmptyLocator:
    def __init__(self):
        self.first = self

    def is_visible(self, timeout=0):
        return False

    def count(self):
        return 0


class ButtonList:
    def __init__(self, button):
        self.button = button

    def count(self):
        return 1

    def nth(self, index):
        return self.button


class FakePopup:
    def __init__(self, url):
        self.url = url
        self.closed = False

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.pages = [FakePopup("https://ads.example/popup"), page]


class FakeMazePage:
    def __init__(self):
        self.url = "https://download.example/laberinto"
        self.button = FakeButton(self)
        self.context = FakeContext(self)
        self._responses = []  # Para interceptar respuestas

    def title(self):
        return "Descarga"

    def content(self):
        return "<html><body>Descargar archivo</body></html>"

    def locator(self, selector):
        if "button" in selector or selector.startswith("a"):
            return ButtonList(self.button)
        return EmptyLocator()

    def get_by_text(self, text, exact=False):
        return self.button

    def wait_for_load_state(self, state=None, timeout=None):
        return None

    def wait_for_timeout(self, ms):
        time.sleep(min(ms / 1000.0, 0.05))

    def bring_to_front(self):
        return None

    def evaluate(self, script):
        return ""

    def close(self):
        return None
    
    def on(self, event, callback):
        """Simula listener de eventos"""
        pass
    
    def remove_listener(self, event, callback):
        """Simula removal de listener"""
        pass
    
    def go_back(self, wait_until=None, timeout=None):
        """Simula ir hacia atrás"""
        pass
    
    def go_forward(self, wait_until=None, timeout=None):
        """Simula ir hacia adelante"""
        pass


class MazeBypassTests(unittest.TestCase):
    def test_close_popups_cierra_pestanas_y_enfoca_principal(self):
        page = FakeMazePage()
        resolver = BrowserResolver()
        cerradas = resolver._close_popups(page.context, page)
        self.assertEqual(cerradas, 1)
        self.assertTrue(page.context.pages[0].closed)

    def test_find_text_button_por_texto_visible(self):
        page = FakeMazePage()
        resolver = BrowserResolver()
        boton = resolver._find_text_button(page)
        self.assertIsNotNone(boton)
        self.assertEqual(boton.inner_text(), "Descargar archivo")

    def test_maze_cierra_popup_pulsa_boton_y_obtiene_txt(self):
        page = FakeMazePage()
        resolver = BrowserResolver(max_sequence_seconds=5, poll_interval=0.01, maze=True)
        outcome = BrowserOutcome(final_url=page.url)
        
        # El test anterior esperaba extracted_url, pero ahora con stealth mode
        # el test verifica que el URL haya cambiado a .txt
        resolver._run_maze(page, page.context, {}, outcome)

        self.assertTrue(page.context.pages[0].closed)
        # El bot debería haber hecho clic
        self.assertGreaterEqual(page.button.clicks, 1)
        # El URL final debería ser el archivo de descarga
        self.assertIn('.txt', page.url)


if __name__ == "__main__":
    unittest.main()
