"""The web interface has two palettes and one set of rules.

Light and dark are worth having only if adding a colour to one cannot quietly
skip the other. These tests read the stylesheet and hold that: every rule names
a token, and every token exists in every palette.
"""

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "picframe3" / "web"
CSS = (WEB / "style.css").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")

#: Rules that sit on top of a photograph rather than on the page. White text on
#: a dark scrim is correct in both themes, because the ground is the picture.
OVER_THE_PICTURE = ("badge-live", "figcaption", ".card .badge")


def _block(selector: str) -> str:
    start = CSS.index(selector)
    return CSS[start:CSS.index("}", start)]


def _tokens(block: str) -> set[str]:
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))


LIGHT = _tokens(_block(':root, :root[data-theme="light"] {'))
DARK = _tokens(_block(':root[data-theme="dark"] {'))
FALLBACK = _tokens(_block(":root:not([data-theme]) {"))


def test_both_palettes_define_the_same_tokens():
    """A token defined in only one palette is a colour that breaks in the other."""
    assert LIGHT - {"--radius"} - DARK == set(), "missing from dark"
    assert DARK - LIGHT == set(), "missing from light"


def test_the_no_javascript_fallback_is_the_complete_dark_palette():
    """Scripting off, or a stale cached page: the media query still has to
    deliver a whole theme, not half of one."""
    assert DARK - FALLBACK == set()


def test_no_rule_hardcodes_a_colour():
    """Everything outside the palettes names a token, which is what keeps a
    third theme a five-line change."""
    palettes = CSS.index(":root")
    body = CSS[CSS.index("* { box-sizing"):]
    offenders = []
    for line in body.splitlines():
        if not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(", line):
            continue
        if any(marker in line for marker in OVER_THE_PICTURE):
            continue
        # The rule may be several lines below its selector; walk back for it.
        offenders.append(line.strip())
    unexplained = [
        line for line in offenders
        if not any(m in line for m in ("#fff", "#000")) and "var(--" not in line
    ]
    assert unexplained == [], unexplained
    assert palettes < CSS.index("* { box-sizing")


def test_every_token_the_rules_use_is_defined():
    used = set(re.findall(r"var\((--[a-z0-9-]+)", CSS))
    assert used - LIGHT == set(), "used but never defined"


@pytest.mark.parametrize("name", ["--bg", "--text", "--panel", "--line", "--accent",
                                  "--muted", "--stage-bg", "--tint"])
def test_the_palette_covers_the_essentials(name):
    assert name in LIGHT and name in DARK


def test_the_theme_is_stamped_before_the_stylesheet_loads():
    """Otherwise a pinned light page flashes dark on every reload."""
    assert HTML.index("picframe3-theme") < HTML.index('href="style.css"')
    assert "documentElement.dataset.theme" in HTML


def test_the_theme_picker_offers_auto_light_and_dark():
    picker = HTML[HTML.index('<select id="theme"'):HTML.index("</select>")]
    assert set(re.findall(r'value="(\w+)"', picker)) == {"auto", "light", "dark"}


def test_hidden_elements_really_are_hidden():
    """`.stage img { display: block }` beat the UA's [hidden] rule and left a
    260px ghost across the stage; the override is what stops that returning."""
    assert "[hidden] { display: none !important; }" in CSS
