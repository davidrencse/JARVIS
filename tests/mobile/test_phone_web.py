"""The phone app's frontend (jarvis/web/phone/): its behaviour in a Node VM with fake DOM, network
and timers (tests/mobile/web_phone_test.cjs), and the static rules the phone server and CSP rely on.
No browser, camera, location or network is touched."""

import json
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "jarvis" / "web"
PHONE = WEB / "phone"
NODE = shutil.which("node")
SCRIPT = Path(__file__).with_name("web_phone_test.cjs")
SCENARIOS = re.findall(r"^  async (\w+)\(\)", SCRIPT.read_text(encoding="utf-8"), re.M)

# The phone server's static allowlist (the spec's "Phone HTTP API"): nothing else is served.
SHELL = {
    "/": PHONE / "index.html", "/phone.css": PHONE / "phone.css", "/phone.js": PHONE / "phone.js",
    "/sw.js": PHONE / "sw.js", "/manifest.webmanifest": PHONE / "manifest.webmanifest",
    "/icons/icon-192.png": PHONE / "icons" / "icon-192.png", "/icons/icon-512.png": PHONE / "icons" / "icon-512.png",
    "/icons/apple-touch-icon.png": PHONE / "icons" / "apple-touch-icon.png", "/icons/icon.svg": PHONE / "icons" / "icon.svg",
    "/fonts/geist.woff2": WEB / "fonts" / "geist.woff2", "/fonts/geist-mono.woff2": WEB / "fonts" / "geist-mono.woff2",
    "/fonts/doto.woff2": WEB / "fonts" / "doto.woff2",
    "/icons.svg": WEB / "icons.svg", "/theme.js": WEB / "theme.js", "/esc.js": WEB / "esc.js",
    "/meters.js": WEB / "meters.js",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def png_header(path: Path):
    """(width, height, colour type) from a PNG's IHDR chunk."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    width, height, _depth, colour = struct.unpack(">IIBB", data[16:26])
    return width, height, colour


@unittest.skipUnless(NODE, "Node.js is required for the phone app's browser tests")
class PhoneAppBehaviourTests(unittest.TestCase):
    def test_service_worker_security(self):
        result = subprocess.run([NODE, str(SCRIPT.with_name("web_phone_sw_test.cjs"))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_scenario(self):
        self.assertGreaterEqual(len(SCENARIOS), 15)
        for name in SCENARIOS:
            with self.subTest(name):
                result = subprocess.run([NODE, str(SCRIPT), name], capture_output=True,
                                        text=True, encoding="utf-8", timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PASS " + name, result.stdout)

    def test_every_script_parses(self):
        for path in [*sorted(PHONE.glob("*.js")), SCRIPT]:
            with self.subTest(path.name):
                result = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)


class PhoneShellTests(unittest.TestCase):
    def test_location_ui_and_geolocation_removed(self):
        html, js = read(PHONE / "index.html"), read(PHONE / "phone.js")
        for token in ('id="tab-radar"', 'id="s-radar"', 'id="d-locate"', 'id="d-maps"'):
            self.assertNotIn(token, html)
        self.assertNotIn("navigator.geolocation", js)
        self.assertNotIn("/phone/api/locate", js)
        self.assertNotIn("maps.apple.com", js)

    def test_every_shell_file_exists(self):
        for url, path in SHELL.items():
            with self.subTest(url):
                self.assertTrue(path.is_file(), path)

    def test_index_has_no_inline_code_and_only_allowlisted_assets(self):
        html = read(PHONE / "index.html")
        for tag in re.findall(r"<script\b[^>]*>", html):
            self.assertRegex(tag, r'\bsrc="/', "every script is a file (CSP script-src 'self')")
        self.assertNotRegex(html, r"<script\b[^>]*>\s*[^<\s]", "no inline script body")
        self.assertNotRegex(html, r"\son[a-z]+\s*=", "no inline event handlers")
        self.assertNotRegex(html, r"\sstyle\s*=", "no inline styles")
        self.assertNotRegex(html, r"https?://(?!www\.w3\.org/2000/svg)", "local assets only")
        for ref in re.findall(r'\b(?:src|href)="([^"#]+)', html):
            with self.subTest(ref):
                self.assertIn(ref, SHELL, "the phone server serves only its allowlist")

    def test_index_carries_the_install_and_viewport_tags(self):
        html = read(PHONE / "index.html")
        self.assertRegex(html, r'name="viewport" content="[^"]*viewport-fit=cover')
        self.assertNotRegex(html, r"user-scalable=no|maximum-scale=1\b")
        for needle in ['rel="manifest" href="/manifest.webmanifest"', 'rel="apple-touch-icon" href="/icons/apple-touch-icon.png"',
                       'name="apple-mobile-web-app-capable" content="yes"',
                       'name="apple-mobile-web-app-status-bar-style" content="black-translucent"',
                       'name="apple-mobile-web-app-title" content="JARVIS"', 'name="theme-color" content="#000000"']:
            self.assertIn(needle, html)

    def test_every_icon_reference_resolves(self):
        html, js = read(PHONE / "index.html"), read(PHONE / "phone.js")
        local = set(re.findall(r'<symbol id="i-([\w-]+)"', html))
        shared = set(re.findall(r'<symbol id="i-([\w-]+)"', read(WEB / "icons.svg")))
        self.assertFalse(local & shared, "the phone sprite only adds what /icons.svg lacks")
        for name in re.findall(r'<use href="#i-([\w-]+)"', html):
            self.assertIn(name, local)
        for name in re.findall(r'<use href="/icons\.svg#i-([\w-]+)"', html):
            self.assertIn(name, shared)
        listed = set(re.findall(r'"([\w-]+)"', re.search(r"LOCAL_ICONS = new Set\(\[(.*?)\]\)", js, re.S).group(1)))
        self.assertEqual(listed, local, "LOCAL_ICONS lists exactly the sprite in index.html")
        used = set(re.findall(r'\bicon\(\s*"([\w-]+)"', js))
        used |= set(re.findall(r'\["\w+", "([\w-]+)", "\w+"\]', re.search(r"const CAPS = (.*?);", js).group(1)))
        used |= set(re.findall(r'"([\w-]+)"\]', re.search(r"const KEYS = (.*?);", js, re.S).group(1))) - {"Esc", "Tab", "Enter"}
        for name in used:
            with self.subTest(name):
                self.assertIn(name, local | shared)

    def test_manifest(self):
        m = json.loads(read(PHONE / "manifest.webmanifest"))
        for key, value in {"id": "/", "start_url": "/", "scope": "/", "display": "standalone", "name": "JARVIS",
                           "short_name": "JARVIS", "background_color": "#000000", "theme_color": "#000000"}.items():
            self.assertEqual(m[key], value, key)
        self.assertNotIn("orientation", m, "landscape must stay possible")
        icons = m["icons"]
        for icon in icons:
            self.assertIn(icon["src"], SHELL)
            self.assertNotIn(" ", icon["purpose"], "never 'any maskable' in one entry")
        anys = {i["sizes"] for i in icons if i["purpose"] == "any" and i["type"] == "image/png"}
        self.assertTrue({"192x192", "512x512"} <= anys)

    def test_icons_are_opaque_pngs_of_the_right_size(self):
        for name, size in {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180}.items():
            with self.subTest(name):
                self.assertEqual(png_header(PHONE / "icons" / name), (size, size, 2), "RGB, no alpha")

    def test_icon_script_draws_what_is_committed(self):
        try:
            from PIL import Image, ImageChops
        except ImportError:
            self.skipTest("Pillow is needed to redraw the icons")
        import importlib.util
        spec = importlib.util.spec_from_file_location("phone_icons", ROOT / "scripts" / "phone_icons.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            mod.main(Path(tmp))
            for name in ("icon-192.png", "icon-512.png", "apple-touch-icon.png"):
                with self.subTest(name), Image.open(Path(tmp) / name) as fresh, Image.open(PHONE / "icons" / name) as kept:
                    self.assertEqual((fresh.size, fresh.mode), (kept.size, kept.mode))
                    low, high = ImageChops.difference(fresh, kept).convert("L").getextrema()
                    self.assertLessEqual(high, 2, "run scripts/phone_icons.py and commit its output")
            self.assertEqual((Path(tmp) / "icon.svg").read_text(encoding="utf-8"), read(PHONE / "icons" / "icon.svg"))

    def test_service_worker_caches_only_the_shell(self):
        sw = read(PHONE / "sw.js")
        self.assertEqual(sw.count("__SHELL_VERSION__"), 2, "the cache name and the comment explaining it")
        self.assertIn('"jarvis-phone-__SHELL_VERSION__"', sw)
        self.assertIn("skipWaiting()", sw)
        self.assertIn("clients.claim()", sw)
        self.assertIn('startsWith("/phone/")', sw)
        self.assertNotRegex(sw, r'addEventListener\("(push|sync|periodicsync|backgroundfetch\w*)"')
        listed = set(re.findall(r'"(/[^"]*)"', re.search(r"const SHELL = \[(.*?)\];", sw, re.S).group(1)))
        self.assertEqual(listed, set(SHELL) - {"/sw.js"})

    def test_phone_js_never_parses_server_text_as_html(self):
        js = read(PHONE / "phone.js")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "createContextualFragment"):
            self.assertNotIn(sink, js, sink)
        self.assertNotIn("navigator.vibrate", js)
        hosts = set(re.findall(r"https?://([\w.-]+)", js))
        self.assertEqual(hosts, {"www.w3.org"}, "no third-party calls or location links")

    def test_stylesheet_uses_local_fonts_and_respects_reduced_motion(self):
        css = read(PHONE / "phone.css")
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertNotRegex(css, r"url\(\s*['\"]?https?:")
        for url in re.findall(r'url\("([^"]+)"\)', css):
            self.assertIn(url, SHELL)
        self.assertIn("touch-action: none", css)
        self.assertIn("-webkit-touch-callout: none", css)
        # 16 px text fields: iOS zooms the page into anything smaller.
        self.assertRegex(css, r"\.field input, \.type input, \.ask-bar textarea \{[^}]*font: 400 16px")


if __name__ == "__main__":
    unittest.main()
