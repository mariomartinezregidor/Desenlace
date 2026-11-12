import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import plugins  # noqa: E402
from desenlace import detect_action, looks_like_shortener, normalize_url  # noqa: E402


class PluginRegistryTests(unittest.TestCase):
    def test_plugins_are_discovered_and_sorted(self):
        discovered = plugins.discover_plugins()
        self.assertGreaterEqual(len(discovered), 10)
        priorities = [plugin.priority for plugin in discovered]
        self.assertEqual(priorities, sorted(priorities))

        names = plugins.plugin_names()
        for expected in ("redirectors", "adlinkfly", "wp-safelink", "adfly"):
            self.assertIn(expected, names)

    def test_registry_aggregates_hosts_and_markers(self):
        hosts = plugins.shortener_hosts()
        self.assertIn("softurl.in", hosts)
        self.assertIn("ouo.io", hosts)
        self.assertIn("adf.ly", hosts)
        self.assertTrue(looks_like_shortener("<html></html>", "https://shrinkme.io/abc"))
        self.assertFalse(
            looks_like_shortener("<html>blog normal</html>", "https://blog.example/x")
        )

    def test_normalize_url_uses_plugin(self):
        self.assertEqual(
            normalize_url("https://pixeldrain.com/u/abc123"),
            "https://pixeldrain.com/api/file/abc123?download",
        )

    def test_detect_action_uses_url_plugins(self):
        action = detect_action(
            "<html></html>",
            "https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Ffinal",
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "goto")
        self.assertEqual(action.url, "https://example.com/final")

    def test_every_plugin_has_a_capability(self):
        for plugin in plugins.discover_plugins():
            self.assertTrue(
                plugin.detect
                or plugin.detect_url
                or plugin.normalize
                or plugin.hosts
                or plugin.markers,
                f"{plugin.name} no hace nada",
            )


if __name__ == "__main__":
    unittest.main()
