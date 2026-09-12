"""Caption and clock layout — the text the frame writes over a picture."""

from picframe3.gfx import overlays
from picframe3.gfx.textstyle import TextStyle
from picframe3.media.metadata import PhotoMeta

STYLE = TextStyle(size=24)
SCREEN = (1280, 720)


def test_single_caption_sits_at_the_bottom():
    placement = overlays.info_bar(["Ordesa", "14 July 2024"], SCREEN, STYLE)
    assert placement is not None
    assert placement.x == 0
    assert placement.y + placement.image.height == SCREEN[1]
    assert placement.image.width == SCREEN[0]


def test_a_portrait_pair_gets_one_caption_each():
    """picframe wrote a caption under each half of a pair; so must this."""
    import numpy as np

    placement = overlays.info_bar(
        [["Left picture", "2024"], ["Right picture", "2025"]], SCREEN, STYLE,
        scrim_opacity=0,          # measure the text, not the gradient behind it
    )
    assert placement is not None
    alpha = np.asarray(placement.image)[..., 3]
    left = alpha[:, : SCREEN[0] // 2].max()
    right = alpha[:, SCREEN[0] // 2 :].max()
    assert left > 200 and right > 200, "text missing from one half"

    # and each caption is centred in its own half, not spread across the middle
    columns = np.where(alpha.max(axis=0) > 128)[0]
    assert columns.min() > 40
    assert columns.max() < SCREEN[0] - 40


def test_empty_captions_draw_nothing():
    assert overlays.info_bar([], SCREEN, STYLE) is None
    assert overlays.info_bar([[], []], SCREEN, STYLE) is None


def test_one_empty_side_still_renders_the_other():
    placement = overlays.info_bar([["Only this one"], []], SCREEN, STYLE)
    assert placement is not None


def test_clock_positions():
    for position, (left, top) in {
        "TL": (True, True), "TR": (False, True),
        "BL": (True, False), "BR": (False, False),
    }.items():
        p = overlays.clock(SCREEN, TextStyle(size=60), position=position)
        assert p is not None
        assert (p.x < SCREEN[0] / 2) is left, position
        assert (p.y < SCREEN[1] / 2) is top, position


def test_clock_extra_line_makes_it_taller():
    plain = overlays.clock(SCREEN, TextStyle(size=60), fmt="%H:%M")
    with_extra = overlays.clock(SCREEN, TextStyle(size=60), fmt="%H:%M",
                                extra="12°C · rain later")
    assert with_extra.image.height > plain.image.height


def test_info_lines_pick_the_requested_fields():
    meta = PhotoMeta(path="/photos/trip/DSC_0042.jpg", title="Ordesa",
                     make="Canon", model="Canon EOS R6", f_number=2.8,
                     exposure_time="1/250s", iso=400, focal_length=50.0,
                     taken_at=1720974125.0)
    lines = overlays.format_info_lines(
        meta, ["title", "name", "folder", "camera", "exposure"],
    )
    assert lines[0] == "Ordesa"
    assert "DSC_0042" in lines[1]
    assert lines[2] == "trip"
    assert lines[3] == "Canon EOS R6", "the doubled maker name should collapse"
    assert lines[4] == "f/2.8 1/250s ISO 400 50mm"


def test_paused_is_appended():
    meta = PhotoMeta(path="/a/b.jpg", title="X")
    assert overlays.format_info_lines(meta, ["title"], paused=True)[-1] == "PAUSED"


# -- caption elements ------------------------------------------------------
# "What is written over the photograph" is a user-facing choice made in the
# Settings tab, so the list of available elements and the order the user puts
# them in both have to be honoured exactly.

def _rich_meta():
    return PhotoMeta(path="/photos/Normandy/lobster.jpg", title="Lunch",
                     caption="Carteret", make="Apple", model="iPhone 15",
                     f_number=1.6, exposure_time="1/2300s", iso=50,
                     focal_length=5.96, taken_at=1720974125.0)


def test_every_offered_caption_element_actually_produces_something():
    """Nothing may be offered in Settings that the frame then ignores."""
    meta = _rich_meta()
    for name, label in overlays.CAPTION_FIELDS:
        lines = overlays.format_info_lines(meta, [name], location="Carteret, France")
        assert lines, f"{name} ({label}) produced no text"


def test_caption_elements_are_written_in_the_order_chosen():
    meta = _rich_meta()
    forward = overlays.format_info_lines(meta, ["date", "location"],
                                         location="Carteret")
    reverse = overlays.format_info_lines(meta, ["location", "date"],
                                         location="Carteret")
    assert forward == list(reversed(reverse))


def test_the_separator_is_configurable():
    style = TextStyle(size=28)
    one_line = overlays.info_bar(["Lunch", "Carteret"], SCREEN, style, separator=" · ")
    stacked = overlays.info_bar(["Lunch", "Carteret"], SCREEN, style, separator="\n")
    assert stacked.image.height > one_line.image.height, \
        "a newline separator should put each element on its own line"


# -- place names -----------------------------------------------------------

def test_place_name_detail_presets():
    """How much of an address a caption shows is a setting, not a constant."""
    from picframe3.media import geocode

    address = {"tourism": "Plage de Hattainville", "village": "Hattainville",
               "county": "Cherbourg", "state": "Normandy", "country": "France"}

    def render(detail, suppress=()):
        coder = geocode.Geocoder.__new__(geocode.Geocoder)
        coder.key_order = list(geocode.key_order_for(detail))
        coder.suppress = list(suppress)
        return coder._format(address)

    assert render("full") == ("Plage de Hattainville, Hattainville, "
                              "Cherbourg, Normandy, France")
    assert render("town_region_country") == "Hattainville, Normandy, France"
    assert render("town_country") == "Hattainville, France"
    assert render("town") == "Hattainville"
    assert render("region_country") == "Normandy, France"
    assert render("country") == "France"
    assert render("town_country", suppress=["France"]) == "Hattainville"
    assert render("nonsense-preset") == render("full"), "a typo must not blank the caption"


def test_every_offered_place_name_preset_resolves():
    from picframe3.media import geocode

    for name in geocode.DETAIL_LABELS:
        assert geocode.key_order_for(name), name


def test_format_address_is_first_key_present_per_tier():
    """The rule the tiers editor teaches, pinned."""
    from picframe3.media.geocode import EXAMPLE_ADDRESS, format_address

    assert format_address(EXAMPLE_ADDRESS, [["tourism"], ["village"], ["country"]]) \
        == "Plage de Hattainville, Baubigny, France"
    # The rest of a line is skipped once one key in it has matched.
    assert format_address(EXAMPLE_ADDRESS, [["village", "town", "city"]]) == "Baubigny"
    # A key the address does not have simply falls through to the next.
    assert format_address(EXAMPLE_ADDRESS, [["isolated_dwelling", "village"]]) == "Baubigny"
    # Nothing matched anywhere: no caption rather than an empty one.
    assert format_address(EXAMPLE_ADDRESS, [["nothing_like_this"]]) is None
    # A value already written is not repeated by a later tier.
    assert format_address({"village": "Baubigny", "town": "Baubigny"},
                          [["village"], ["town"]]) == "Baubigny"
    assert format_address(EXAMPLE_ADDRESS, [["village"], ["country"]],
                          suppress=["France"]) == "Baubigny"


def test_every_key_the_editor_offers_is_a_real_nominatim_key():
    """The chips have to be usable, not decorative."""
    from picframe3.media import geocode

    offered = {key for _, keys in geocode.NOMINATIM_KEYS for key in keys}
    assert len(offered) == sum(len(keys) for _, keys in geocode.NOMINATIM_KEYS), \
        "a key is listed under two groups"
    # Every key any preset relies on must be offered, or a preset could not be
    # rebuilt by hand.
    for name in geocode.DETAIL_PRESETS:
        for tier in geocode.key_order_for(name):
            assert set(tier) <= offered, (name, set(tier) - offered)


def test_the_example_address_exercises_the_rule():
    """It is what the settings page shows before any picture has been on
    screen, so it has to have both a hit and a miss in it."""
    from picframe3.media import geocode

    assert "village" in geocode.EXAMPLE_ADDRESS
    assert "isolated_dwelling" not in geocode.EXAMPLE_ADDRESS
    assert geocode.format_address(geocode.EXAMPLE_ADDRESS,
                                  geocode.key_order_for("full"))


def test_a_malformed_clock_position_does_not_reach_the_draw_path():
    """A hand-edited "top-right" used to be an IndexError frames later."""
    from picframe3.gfx import overlays

    assert overlays.normalise_position("bl") == "BL"
    assert overlays.normalise_position(" tc ") == "TC"
    for nonsense in ("", None, "T", "top-right", "XY", 7):
        assert overlays.normalise_position(nonsense) == overlays.DEFAULT_POSITION

    style = TextStyle(size=20)
    placed = overlays.clock((800, 480), style, position="top-right", now=0)
    assert placed is not None and 0 <= placed.x < 800 and 0 <= placed.y < 480
