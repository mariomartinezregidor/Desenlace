import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desenlace import (  # noqa: E402
    SHORTENER_HOSTS,
    ResolveError,
    decode_adfly_ysmm,
    decode_embedded_url,
    detect_action,
    ensure_extension,
    extract_redirect_target,
    host_matches,
    load_cookie_file,
    looks_like_shortener,
    next_url_from_payload,
    normalize_file_host_url,
    parse_counter,
    parse_extra_headers,
    unpack_javascript,
)


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def adfly_encode(url: str) -> str:
    """Genera un token ysmm equivalente al que usa adf.ly."""
    marker = "0123456789abcdef"
    decoded = (marker + url + marker).encode()
    encoded = base64.b64encode(decoded).decode()
    half = (len(encoded) + 1) // 2
    left, right = encoded[:half], encoded[half:]
    odd = list(reversed(right))
    result = []
    for index in range(max(len(left), len(odd))):
        if index < len(left):
            result.append(left[index])
        if index < len(odd):
            result.append(odd[index])
    return "".join(result)


class HostMatchingTests(unittest.TestCase):
    def test_domain_boundaries(self):
        self.assertTrue(host_matches("softurl.in", SHORTENER_HOSTS))
        self.assertTrue(host_matches("www.shrinkme.io", SHORTENER_HOSTS))
        self.assertFalse(host_matches("notsofturl.in", SHORTENER_HOSTS))
        self.assertFalse(host_matches("softurl.in.evil.test", SHORTENER_HOSTS))

    def test_new_shortener_hosts_detected(self):
        for url in (
            "https://shrinkme.io/abc",
            "https://gplinks.co/abc",
            "https://ouo.io/abc",
            "https://linkvertise.com/123",
            "https://adf.ly/abc",
        ):
            self.assertTrue(looks_like_shortener("<html></html>", url))

    def test_article_mentioning_safelink_is_not_shortener(self):
        html = (
            "<html><body><article>Qué es un safelink y cómo funciona</article>"
            "<script>msgtext.textContent = `Espera ${counter} segundos`;</script>"
            "</body></html>"
        )
        self.assertFalse(looks_like_shortener(html, "https://blog.example/que-es-safelink"))


class RedirectTargetTests(unittest.TestCase):
    def test_social_redirectors(self):
        cases = {
            "https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&h=AT0":
                "https://example.com/a?b=1",
            "https://out.reddit.com/t3_abc?url=https%3A%2F%2Fexample.com%2Fpost":
                "https://example.com/post",
            "https://away.vk.com/away.php?to=https%3A%2F%2Fexample.com%2Fvk":
                "https://example.com/vk",
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fddg&rut=x":
                "https://example.com/ddg",
        }
        for source, expected in cases.items():
            self.assertEqual(extract_redirect_target(source), expected)

    def test_bing_base64_param(self):
        encoded = b64("https://example.com/bing")
        self.assertEqual(
            extract_redirect_target(f"https://www.bing.com/ck/a?u=a1{encoded}"),
            "https://example.com/bing",
        )

    def test_generic_gateway_with_base64(self):
        encoded = b64("https://example.com/gateway")
        self.assertEqual(
            extract_redirect_target(f"https://softurl.in/redirect?url={encoded}"),
            "https://example.com/gateway",
        )

    def test_generic_param_outside_gateway_is_ignored(self):
        self.assertIsNone(
            extract_redirect_target("https://example.com/?url=https://a.test/x")
        )

    def test_encoded_value_decoding(self):
        self.assertEqual(
            decode_embedded_url("https%3A%2F%2Fexample.com%2Fdoble"),
            "https://example.com/doble",
        )
        self.assertEqual(
            decode_embedded_url(b64("https://example.com/b64")),
            "https://example.com/b64",
        )


class DetectorTests(unittest.TestCase):
    def test_iframe_redirect_in_shortener(self):
        html = '<html><body><iframe src="https://example.com/file.zip"></iframe></body></html>'
        action = detect_action(html, "https://softurl.in/abc")
        self.assertIsNotNone(action)
        self.assertEqual(action.url, "https://example.com/file.zip")

    def test_data_attribute_with_base64(self):
        encoded = b64("https://example.com/final")
        html = (
            '<html><body><form id="go-link"></form>'
            f'<button data-url="{encoded}">Continuar</button></body></html>'
        )
        action = detect_action(html, "https://softurl.in/abc")
        self.assertIsNotNone(action)
        self.assertEqual(action.url, "https://example.com/final")

    def test_known_button_selector(self):
        html = '<html><body><a id="btn-main" href="/go/next">Seguir</a></body></html>'
        action = detect_action(html, "https://ouo.io/abc")
        self.assertIsNotNone(action)
        self.assertEqual(action.url, "https://ouo.io/go/next")

    def test_adlinkfly_clone_without_ad_form_data(self):
        html = (
            '<html><body><form id="go-link" action="/links/go" method="post">'
            '<input type="hidden" name="_csrfToken" value="tok"/>'
            "</form></body></html>"
        )
        action = detect_action(html, "https://gplinks.co/abc")
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "xhr_post")
        self.assertEqual(action.url, "https://gplinks.co/links/go")

    def test_wp_safelink_in_query(self):
        payload = {"delay": "7", "url": "https://example.com/from-query"}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        action = detect_action(
            "<html><body>Espera…</body></html>",
            f"https://safelink.example/?safelink={encoded}",
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "wait_get")
        self.assertEqual(action.delay, 7.0)
        self.assertEqual(action.url, "https://example.com/from-query")

    def test_generic_get_form_becomes_goto(self):
        html = (
            '<html><body><form method="get" action="/download">'
            '<input type="hidden" name="id" value="42"/>'
            '<input type="hidden" name="confirm" value="t"/>'
            "</form></body></html>"
        )
        action = detect_action(html, "https://drive.google.com/uc?export=download")
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "goto")
        self.assertIn("id=42", action.url)
        self.assertIn("confirm=t", action.url)

    def test_adfly_ysmm_roundtrip(self):
        target = "https://example.com/archivo.zip"
        token = adfly_encode(target)
        self.assertEqual(decode_adfly_ysmm(token), target)

    def test_fromcharcode_redirect(self):
        target = "https://example.com/charcode"
        codes = ",".join(str(ord(char)) for char in target)
        html = f"<script>location.href = String.fromCharCode({codes});</script>"
        action = detect_action(html, "https://softurl.in/a")
        self.assertIsNotNone(action)
        self.assertEqual(action.url, target)

    def test_unescape_redirect(self):
        html = (
            "<script>location.href = unescape("
            '"%68%74%74%70%73%3A%2F%2Fexample.com%2Funescape");</script>'
        )
        action = detect_action(html, "https://softurl.in/a")
        self.assertIsNotNone(action)
        self.assertEqual(action.url, "https://example.com/unescape")

    def test_packed_javascript(self):
        html = (
            "<script>eval(function(p,a,c,k,e,d){e=function(c){return c};"
            "if(!''.replace(/^/,String)){while(c--){d[c]=k[c]};"
            "k=[function(e){return d[e]}];e=function(){return'\\\\w+'};c=1};"
            "while(c--){if(k[c]){p=p.replace(new RegExp('\\\\b'+e(c)+'\\\\b','g'),k[c])}}"
            "return p}('var a=\"4:\\\\/\\\\/0.2.1\\\\/3\";',5,5,"
            "'file|com|example|https|http'.split('|'),0,{}))</script>"
        )
        self.assertIsNotNone(unpack_javascript(html))
        action = detect_action(html, "https://softurl.in/abc")
        self.assertIsNotNone(action)
        self.assertEqual(action.url, "http://file.example.com/https")


class FileHostTests(unittest.TestCase):
    def test_google_drive(self):
        self.assertEqual(
            normalize_file_host_url(
                "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQr/view?usp=sharing"
            ),
            "https://drive.google.com/uc?export=download&id=1AbCdEfGhIjKlMnOpQr",
        )

    def test_google_docs_export(self):
        self.assertEqual(
            normalize_file_host_url(
                "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQr/edit"
            ),
            "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQr/export?format=docx",
        )

    def test_dropbox(self):
        self.assertEqual(
            normalize_file_host_url("https://www.dropbox.com/s/abc/file.zip?dl=0"),
            "https://dl.dropboxusercontent.com/s/abc/file.zip?dl=1",
        )

    def test_pixeldrain(self):
        self.assertEqual(
            normalize_file_host_url("https://pixeldrain.com/u/abc123"),
            "https://pixeldrain.com/api/file/abc123?download",
        )

    def test_untouched_url(self):
        url = "https://example.com/pagina"
        self.assertEqual(normalize_file_host_url(url), url)


class MiscParsingTests(unittest.TestCase):
    def test_next_url_payload_variants(self):
        self.assertEqual(next_url_from_payload({"url": "https://a.test/1"}), "https://a.test/1")
        self.assertEqual(next_url_from_payload({"link": "/go/2"}), "/go/2")
        self.assertEqual(
            next_url_from_payload({"status": "success", "data": {"url": "https://a.test/3"}}),
            "https://a.test/3",
        )
        self.assertIsNone(next_url_from_payload({"message": "error"}))

    def test_parse_counter_new_patterns(self):
        self.assertEqual(parse_counter("<script>var countdown = 12;</script>"), 12.0)
        self.assertEqual(parse_counter('<div data-timer="9"></div>'), 9.0)
        self.assertEqual(parse_counter('<span id="timer">4.5</span>'), 4.5)

    def test_ensure_extension(self):
        self.assertEqual(ensure_extension("archivo", "application/pdf"), "archivo.pdf")
        self.assertEqual(ensure_extension("archivo.zip", "application/pdf"), "archivo.zip")
        self.assertEqual(ensure_extension("archivo", "text/html"), "archivo")

    def test_parse_extra_headers(self):
        self.assertEqual(
            parse_extra_headers(["Accept-Language: es", "X-Test: 1:2"]),
            {"Accept-Language": "es", "X-Test": "1:2"},
        )
        with self.assertRaises(ResolveError):
            parse_extra_headers(["sin-dos-puntos"])

    def test_load_cookie_file_simple(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("# comentario\nCookie: alpha=1; beta=2\n")
            path = Path(handle.name)
        try:
            jar = load_cookie_file(path)
            self.assertEqual(jar.get("alpha"), "1")
            self.assertEqual(jar.get("beta"), "2")
        finally:
            path.unlink(missing_ok=True)

    def test_load_cookie_file_netscape(self):
        content = (
            "# Netscape HTTP Cookie File\n"
            ".example.com\tTRUE\t/\tFALSE\t0\tsession\tabc123\n"
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(content)
            path = Path(handle.name)
        try:
            jar = load_cookie_file(path)
            self.assertEqual(jar.get("session", domain=".example.com"), "abc123")
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
