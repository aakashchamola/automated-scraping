"""The published page's stylesheet, checked for the mistakes CSS makes silently.

CSS has no undefined-variable error. `var(--card, #fff)` against a token that
was never defined renders the fallback and says nothing — which is how the
project switcher shipped with a white background while its text inherited the
dark theme's near-white colour. It was invisible, and every test passed.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "site", "style.css")


def read_css():
    with open(CSS, encoding="utf-8") as fh:
        return fh.read()


def defined_tokens(css):
    """Every custom property the stylesheet defines, in any block."""
    return set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", css, re.M))


def used_tokens(css):
    """Every custom property the stylesheet reads, with its fallback if any."""
    return re.findall(r"var\(\s*(--[a-z0-9-]+)\s*(,[^)]*)?\)", css)


class Tokens(unittest.TestCase):

    def test_every_variable_used_is_defined(self):
        css = read_css()
        defined = defined_tokens(css)
        missing = sorted({name for name, _ in used_tokens(css)} - defined)
        self.assertEqual(
            missing, [],
            "these are read but never defined, so they silently render their "
            f"fallback in BOTH themes: {missing}")

    def test_no_colour_falls_back_to_a_literal(self):
        # A literal fallback is a light-mode value frozen into a themed page.
        # Where the token exists it is dead code; where it does not, it is the
        # bug above wearing a disguise.
        offenders = []
        for name, fallback in used_tokens(read_css()):
            if fallback and re.search(r"#[0-9a-fA-F]{3,8}|rgb|hsl", fallback):
                offenders.append(f"{name}{fallback}")
        self.assertEqual(offenders, [],
                         f"colour fallbacks hide theme bugs: {offenders}")

    def test_the_dark_theme_repoints_the_surface_tokens(self):
        css = read_css()
        dark = css[css.index("prefers-color-scheme: dark"):]
        for token in ("--bg", "--surface", "--text", "--border"):
            with self.subTest(token=token):
                self.assertRegex(dark, rf"{token}\s*:",
                                 f"{token} is never re-pointed for dark mode")

    def test_hidden_beats_the_layout(self):
        # A .gate that stays display:flex while [hidden] is set leaves an
        # invisible overlay swallowing every click. This has happened.
        css = read_css()
        self.assertRegex(css, r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important")


class SwitcherIsThemed(unittest.TestCase):
    """The switcher and dialog were added later and missed the token pass."""

    def selector_block(self, selector):
        css = read_css()
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        self.assertIsNotNone(match, f"{selector} not found in style.css")
        return match.group(1)

    def test_the_menu_states_its_own_colours(self):
        block = self.selector_block(".menu")
        # Inheriting a colour onto a surface that sets its own background is
        # exactly how white-on-white happens.
        for prop in ("background", "color", "border"):
            with self.subTest(prop=prop):
                self.assertIn(prop, block)

    def test_menu_items_state_a_colour(self):
        self.assertIn("color: var(--text)", self.selector_block(".menu-item"))

    def test_the_dialog_states_its_own_colours(self):
        block = self.selector_block("#new-project")
        self.assertIn("background: var(--surface)", block)
        self.assertIn("color: var(--text)", block)


if __name__ == "__main__":
    unittest.main()
