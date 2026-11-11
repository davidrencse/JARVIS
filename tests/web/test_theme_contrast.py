"""State colours as small text must reach WCAG AA (4.5:1) on every surface they sit on, in both
themes. Computed from the real CSS token declarations (cascade order and specificity), not from a
screenshot."""

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "jarvis" / "web"
STATE_TOKENS = ("--ok", "--warn", "--alert", "--info")
AA = 4.5

LIGHT = {":root", ':root[data-theme="light"]'}
DARK = {':root[data-theme="dark"]'}  # applied over the default (light) root values


def _css_files():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    return [WEB / href for href in re.findall(r'<link rel="stylesheet" href="([^"?]+)', html)
            if (WEB / href).exists()]


def _tokens(theme, only=None):
    """token -> value after the cascade for :root rules that apply to `theme`. `only` limits it to
    one stylesheet (porcelain.css's own declared surfaces, the ones the audit measured)."""
    accepted = LIGHT if theme == "light" else LIGHT | DARK
    found = {}  # token -> (specificity, order, value)
    order = 0
    for path in _css_files():
        if only and path.name != only:
            continue
        text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            hits = [s.strip() for s in selectors.split(",") if s.strip() in accepted]
            if not hits:
                continue
            spec = max(2 if "[" in s else 1 for s in hits)
            if theme == "dark" and not any(s in DARK for s in hits) and spec == 2:
                continue  # a [data-theme=light] rule doesn't apply in dark
            for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);?", body):
                order += 1
                if name not in found or (spec, order) >= found[name][:2]:
                    found[name] = (spec, order, value.strip())
    return {k: v[2] for k, v in found.items()}


def _rgba(value):
    value = value.strip()
    if value == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    m = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", value)
    if m:
        h = m.group(1)
        h = "".join(c * 2 for c in h) if len(h) == 3 else h
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (1.0,)
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", value)
    if m:
        r, g, b, a = m.groups()
        return (float(r), float(g), float(b), float(a) if a is not None else 1.0)
    raise AssertionError(f"unparseable colour {value!r}")


def _over(top, under):
    r, g, b, a = top
    return tuple(a * f + (1 - a) * u for f, u in zip((r, g, b), under[:3])) + (1.0,)


def _lum(rgb):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def surfaces(tok):
    bg = _rgba(tok["--bg"])
    card = _over(_rgba(tok["--card"]), bg)
    out = {"bg": bg, "card": card, "card-strong": _over(_rgba(tok["--card-strong"]), bg)}
    for name, base in (("bg", bg), ("card", card)):
        out[f"sunk/{name}"] = _over(_rgba(tok["--card-sunk"]), base)
        out[f"hover/{name}"] = _over(_rgba(tok["--hover"]), base)
    return out


class StateContrast(unittest.TestCase):
    def check(self, theme, only=None):
        tok = _tokens(theme, only)
        for name in STATE_TOKENS:
            self.assertIn(name, tok, f"{theme}: {name} undefined")
        low = []
        for name in STATE_TOKENS:
            fg = _rgba(tok[name])
            for surface, colour in surfaces(tok).items():
                ratio = contrast(fg, colour)
                if ratio < AA:
                    low.append(f"{theme}{'/' + only if only else ''} {name} {tok[name]} on {surface}: {ratio:.2f}")
        self.assertEqual(low, [], "state text below 4.5:1")

    def test_light_state_tokens_reach_aa_on_every_surface(self):
        self.check("light")  # what the cascade paints (console.css skin over porcelain.css)

    def test_light_state_tokens_reach_aa_on_porcelain_surfaces(self):
        self.check("light", "porcelain.css")  # --bg #e7e7e9 etc.: the surfaces the audit measured

    def test_dark_state_tokens_reach_aa_on_every_surface(self):
        self.check("dark")

    def test_dark_state_tokens_reach_aa_on_porcelain_surfaces(self):
        self.check("dark", "porcelain.css")

    def test_computation_matches_known_ratios(self):
        # black on white is 21:1; the audit's old light tokens on --bg were 4.26 and 4.05
        self.assertAlmostEqual(contrast((0, 0, 0), (255, 255, 255)), 21.0, places=1)
        bg = _rgba("#e7e7e9")
        self.assertAlmostEqual(contrast(_rgba("#2f7a45"), bg), 4.26, places=2)
        self.assertAlmostEqual(contrast(_rgba("#9a6400"), bg), 4.05, places=2)

    def test_state_still_reads_as_state_not_ink(self):
        """Darkened, not collapsed into the text colour: hues stay distinct and ok != warn != alert."""
        tok = _tokens("light", "porcelain.css")
        ok, warn, alert = (_rgba(tok[n])[:3] for n in ("--ok", "--warn", "--alert"))
        self.assertGreater(ok[1], ok[0] + 30)       # green still dominant
        self.assertGreater(warn[0], warn[2] + 60)   # amber still red/yellow over blue
        self.assertGreater(alert[0], alert[1] + 60)  # red still red
        ink = _rgba(tok["--ink"])
        for c in (ok, warn, alert):
            self.assertGreater(contrast(c + (1.0,), ink), 1.5)

    def test_no_other_stylesheet_redefines_state_tokens_at_the_theme_root(self):
        for theme in ("light", "dark"):
            tok = _tokens(theme)
            self.assertTrue(all(t in tok for t in STATE_TOKENS))
        defining = []
        for path in _css_files():
            text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
            for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
                if any(s.strip() in LIGHT | DARK for s in selectors.split(",")) and re.search(r"--(?:ok|warn|alert|info)\s*:", body):
                    defining.append(path.name)
        self.assertEqual(set(defining), {"porcelain.css"})


if __name__ == "__main__":
    unittest.main()
