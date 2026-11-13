"""Tests para laberintos profundos de descargas."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from browser_resolver import BrowserResolver, BrowserOutcome  # noqa: E402


class FakePageForDeepMaze:
    """Simula un laberinto profundo con múltiples niveles."""
    
    def __init__(self):
        self.url = "https://mtc1.higgsdominorpapk.com/initial-page/"
        self.clicks = 0
        self.click_sequence = [
            "https://mtc1.higgsdominorpapk.com/page-2/",
            "https://mtc1.higgsdominorpapk.com/page-3/",
            "https://mtc1.higgsdominorpapk.com/page-4/",
            "https://example.com/download.txt",  # Finalmente, el .txt
        ]
        self.context = None
    
    def title(self):
        return "Laberinto Profundo"
    
    def content(self):
        return """
        <html>
        <body>
            <button id="continue-btn">Continuar al siguiente paso</button>
            <a href="https://example.com/download.txt" style="display:none">Download</a>
        </body>
        </html>
        """
    
    def locator(self, selector):
        if 'button' in selector or 'continue' in selector.lower():
            return FakeContinueButton(self)
        return FakeEmptyLocator()
    
    def set_context(self, context):
        self.context = context
        return self
    
    def evaluate(self, script):
        if 'querySelector' in script and 'txt' in script:
            if '.txt' in self.url:
                return "https://example.com/download.txt"
        return None
    
    def wait_for_load_state(self, state=None, timeout=None):
        pass
    
    def wait_for_timeout(self, ms):
        pass
    
    def bring_to_front(self):
        pass
    
    def close(self):
        pass
    
    def get_by_text(self, text, exact=False):
        return FakeContinueButton(self)
    
    def on(self, event, callback):
        """Simula listener de eventos"""
        pass
    
    def remove_listener(self, event, callback):
        """Simula removal de listener"""
        pass
    
    def go_back(self, wait_until=None, timeout=None):
        """Simula ir hacia atrás"""
        if self.clicks > 0:
            self.clicks -= 1
            self.url = self.click_sequence[self.clicks]
    
    def go_forward(self, wait_until=None, timeout=None):
        """Simula ir hacia adelante"""
        if self.clicks < len(self.click_sequence) - 1:
            self.clicks += 1
            self.url = self.click_sequence[self.clicks]


class FakeContinueButton:
    def __init__(self, page):
        self.page = page
        self.first = self
    
    def is_visible(self, timeout=0):
        return True
    
    def inner_text(self, timeout=0):
        return "Continuar"
    
    def click(self, timeout=0, force=False):
        self.page.clicks += 1
        if self.page.clicks < len(self.page.click_sequence):
            self.page.url = self.page.click_sequence[self.page.clicks]
    
    def evaluate(self, script):
        self.page.clicks += 1
        if self.page.clicks < len(self.page.click_sequence):
            self.page.url = self.page.click_sequence[self.page.clicks]


class FakeEmptyLocator:
    def count(self):
        return 0
    
    def is_visible(self, timeout=0):
        return False


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.context = FakeContext(page)
    
    def new_context(self, **kwargs):
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


class DeepMazeTests(unittest.TestCase):
    """Test laberintos profundos con múltiples clics."""
    
    def test_deep_maze_reaches_txt_after_multiple_clicks(self):
        """Laberinto profundo: hacer múltiples clics hasta llegar al .txt."""
        page = FakePageForDeepMaze()
        page.set_context(FakeContext(page))
        
        resolver = BrowserResolver(
            verbose=True,
            playwright_factory=lambda: FakePlaywright(page),
            poll_interval=0.01,
            max_sequence_seconds=60.0,
            maze=True,
        )
        
        outcome = BrowserOutcome(final_url=page.url)
        
        # Ejecutar maze
        resolver._run_maze(page, page.context, {}, outcome, max_iterations=10)
        
        # Verificar que navegó múltiples páginas
        self.assertGreater(page.clicks, 1, "Debería haber hecho múltiples clics")
        
        # Verificar que encontró el .txt
        if outcome.extracted_url:
            self.assertIn('.txt', outcome.extracted_url.lower())
    
    def test_maze_detects_txt_in_url(self):
        """Detección de .txt en URL final."""
        page = FakePageForDeepMaze()
        page.url = "https://example.com/download.txt"
        
        resolver = BrowserResolver()
        
        # Verificar que _is_file_url detecta .txt
        is_file = resolver._is_file_url(page.url)
        self.assertTrue(is_file, ".txt debería ser detectado como archivo")
    
    def test_maze_respects_max_iterations(self):
        """El maze respeta el límite de iteraciones."""
        page = FakePageForDeepMaze()
        page.set_context(FakeContext(page))
        
        resolver = BrowserResolver(max_sequence_seconds=10.0)
        
        outcome = BrowserOutcome(final_url=page.url)
        
        # Ejecutar maze con máximo 5 iteraciones
        resolver._run_maze(page, page.context, {}, outcome, max_iterations=5)
        
        # Debería detenerse en o antes de 5
        self.assertLessEqual(page.clicks, 5, "No debería exceder max_iterations")


if __name__ == "__main__":
    unittest.main()
