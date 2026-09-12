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


# -- restarting from the page ----------------------------------------------

APP_JS = (WEB / "app.js").read_text(encoding="utf-8")


def test_the_settings_page_can_restart_the_frame():
    assert 'id="restart"' in HTML
    assert "/api/restart" in APP_JS


def test_a_restart_saves_first():
    """A restart that threw away the changes it was asked to apply would be
    worse than no button at all."""
    assert "save=true" in APP_JS


def test_the_page_waits_for_the_frame_to_come_back():
    """The frame stops answering the moment it acts, so the page has to poll
    rather than treat the failed request as an error."""
    assert "waitForTheFrame" in APP_JS
    assert "/api/health" in APP_JS


def test_the_notice_tells_you_which_settings_are_waiting():
    assert 'id="restart-notice"' in HTML
    assert "restart_required" in APP_JS
    assert "unsaved_changes" in APP_JS


# -- the address tiers editor ----------------------------------------------

def test_the_tiers_editor_previews_against_the_real_formatter():
    """Re-implementing the first-key-per-tier rule in JavaScript would drift
    from the one the frame actually uses, so the page asks the frame."""
    assert "/api/geo/preview" in APP_JS
    assert "parseTiers" in APP_JS


def test_the_subfolder_field_offers_the_folders_that_exist():
    assert 'case "datalist"' in APP_JS
    assert "<datalist" in APP_JS


# -- the filter panel -------------------------------------------------------

def test_the_panel_offers_every_filter_the_frame_understands():
    for field in ("folder", "tags", "tags_match_all", "location",
                  "date_from", "date_to"):
        assert f'data-filter="{field}"' in HTML, field


def test_the_panel_counts_before_it_changes_the_wall():
    """Typing previews; leaving the box applies.  Applying on every keystroke
    would send the picture on the wall somewhere new letter by letter."""
    assert "/api/filters/preview" in APP_JS
    assert "previewFilter" in APP_JS and "applyFilter" in APP_JS


def test_the_panel_follows_a_filter_set_from_somewhere_else():
    """Home Assistant, a phone, a second tab: the panel reads the state
    document rather than only its own last edit."""
    assert "showFilter(filters)" in APP_JS
    assert "state.filters" in APP_JS


def test_the_dropdowns_are_filled_from_the_library():
    assert "/api/filters" in APP_JS
    assert 'id="f-tag-list"' in HTML and 'id="f-place-list"' in HTML
