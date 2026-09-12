from PIL import Image

from picframe3.media import mat, prepare
from picframe3.media.metadata import PhotoMeta


def _photo(w, h, colour=(180, 120, 70)):
    return Image.new("RGB", (w, h), colour)


def test_should_mat_only_on_shape_mismatch():
    assert mat.should_mat((900, 1200), (1920, 1080), 0.01)      # portrait on landscape
    assert not mat.should_mat((1920, 1080), (1920, 1080), 0.01)  # exact fit
    assert mat.should_mat((1920, 1080), (1920, 1080), -1)        # -1 = always


def test_mat_fills_the_screen_exactly():
    out = mat.apply([_photo(900, 1200)], (1920, 1080), mat.MatStyle(style="double"))
    assert out.size == (1920, 1080)


def test_mat_colour_is_the_photograph_s_own_colour():
    """Not a pale board tinted towards it — the colour itself.

    Washing it out was the regression: every photograph then produced the same
    off-white mat, which is the opposite of extracting a colour.
    """
    warm = mat.auto_colors(_photo(200, 200, (200, 80, 40)))[0]
    cool = mat.auto_colors(_photo(200, 200, (40, 80, 200)))[0]
    assert warm != cool
    assert max(warm) - min(warm) > 80, "the mat lost the photograph's saturation"
    assert warm[0] > warm[2] and cool[2] > cool[0], "hue should survive"


def test_the_most_saturated_cluster_wins_not_the_biggest():
    """A grey photograph with a small red subject gets a red mat.

    picframe ranked the k-means centroids by saturation for exactly this
    reason; ranking by pixel count gives you the sky every time.
    """
    from PIL import ImageDraw

    img = Image.new("RGB", (100, 100), (150, 152, 155))       # 80% flat grey
    ImageDraw.Draw(img).rectangle([0, 80, 100, 100], fill=(190, 45, 40))
    r, g, b = mat.dominant_color(img)
    assert r > 150 and g < 90 and b < 90, f"got {(r, g, b)} instead of the red"


def test_the_inner_mat_is_half_the_outer():
    outer, inner = mat.auto_colors(_photo(200, 200, (200, 80, 40)))
    assert all(abs(i - o * 0.5) <= 1 for o, i in zip(outer, inner, strict=True))


def test_the_board_uses_the_shipped_paper_scan():
    """The texture is the look people recognise; a flat fill is not it."""
    assert mat.TEXTURE_FILE.exists(), "mat_texture.jpg is missing from the package"
    texture = mat.board_texture((320, 180))
    assert texture is not None and texture.mode == "L"
    assert texture.size == (320, 180)

    import numpy as np

    # Measured at panel size: the scan is 2560x1440, and squeezing it into a
    # postage stamp averages its grain away, which would test the resampler
    # rather than the board.
    panel = (1920, 1080)
    textured = mat.apply([_photo(900, 1200)], panel,
                         mat.MatStyle(style="single", texture=True))
    flat = mat.apply([_photo(900, 1200)], panel,
                     mat.MatStyle(style="single", texture=False))
    # A strip of pure board down the left edge, clear of the print and of the
    # hairline around it.
    # Per channel: a whole-array std is dominated by the gap between R, G and B
    # rather than by the variation within each of them, which is the grain.
    strip = lambda im: np.asarray(im.crop((8, 8, 120, 1072)),  # noqa: E731
                                  dtype=float).std(axis=(0, 1)).mean()
    assert strip(flat) < 0.01, "a flat fill should be perfectly flat"
    assert strip(textured) > 2.0, "no grain in the board"


def test_a_missing_texture_file_falls_back_instead_of_failing():
    out = mat.apply([_photo(900, 1200)], (320, 180),
                    mat.MatStyle(style="single", texture_file="/nope/missing.jpg"))
    assert out.size == (320, 180)


def test_every_mat_style_renders():
    for style in mat.STYLES:
        out = mat.apply([_photo(900, 1200)], (800, 480), mat.MatStyle(style=style),
                        seed=style)
        assert out.size == (800, 480)


def test_picframe_style_names_are_accepted():
    """A migrated config must keep working."""
    import random

    rng = random.Random(0)
    for old, new in mat.ALIASES.items():
        assert mat.MatStyle(style=old).resolved_style(rng) == new


def test_a_list_of_styles_picks_one_of_them():
    import random

    chosen = {
        mat.MatStyle(style="float polaroid double_flat").resolved_style(random.Random(i))
        for i in range(30)
    }
    assert chosen <= {"float", "polaroid", "double"}
    assert len(chosen) > 1, "a list should actually vary"


def test_unknown_style_falls_back_without_raising(caplog):
    import random

    assert mat.MatStyle(style="nonsense").resolved_style(random.Random(0)) == "single"


def test_styles_differ_from_each_other():
    """Guards against a refactor quietly collapsing two styles into one."""
    import hashlib

    photo = _photo(900, 1200)
    digests = {
        style: hashlib.sha1(
            mat.apply([photo], (640, 360), mat.MatStyle(style=style), seed="x").tobytes()
        ).hexdigest()
        for style in mat.STYLES
    }
    assert len(set(digests.values())) == len(mat.STYLES), digests


def test_dominant_colour_survives_a_bimodal_image():
    """An average would turn sunset-over-forest into mud; k-means must not."""
    from PIL import Image

    img = Image.new("RGB", (100, 100), (20, 60, 30))       # dark forest
    img.paste(Image.new("RGB", (100, 40), (230, 120, 40)), (0, 0))  # orange sky
    r, g, b = mat.dominant_color(img)
    assert not (abs(r - g) < 25 and abs(g - b) < 25), "picked a grey average"


def test_prepare_modes_produce_screen_sized_images(tmp_path):
    path = tmp_path / "p.jpg"
    _photo(1600, 900).save(path)
    meta = PhotoMeta(path=str(path), width=1600, height=900)
    for fit in ("cover", "contain", "blur", "mat", "auto"):
        out = prepare.prepare([meta], (1280, 720), prepare.PrepareOptions(fit=fit))
        assert out.size == (1280, 720), fit


def test_kenburns_headroom_renders_larger_than_the_screen(tmp_path):
    path = tmp_path / "p.jpg"
    _photo(2000, 1400).save(path)
    meta = PhotoMeta(path=str(path), width=2000, height=1400)
    out = prepare.prepare([meta], (1280, 720),
                          prepare.PrepareOptions(fit="cover", kenburns_headroom=1.2))
    assert out.size == (1536, 864)


def test_portrait_pairing_keeps_both_whole():
    a, b = _photo(600, 1000), _photo(500, 1400)
    paired = prepare.pair_portraits(a, b, gap=10)
    assert paired.height == 1000
    assert paired.width > a.width


def test_unreadable_file_returns_none(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a jpeg")
    assert prepare.prepare([PhotoMeta(path=str(broken))], (640, 480),
                           prepare.PrepareOptions()) is None


def test_placeholder_is_drawn():
    img = prepare.placeholder((640, 480), "No pictures", "looked in ~/Pictures")
    assert img.size == (640, 480)


def test_the_empty_screen_is_a_picture_by_default():
    """An empty picture frame should still have a picture in it."""
    assert prepare.NO_FILES_FILE.exists(), "the shipped no_pictures.jpg is missing"
    shown = prepare.no_files_screen((640, 480))
    assert shown.size == (640, 480)
    assert list(shown.getdata()) != list(prepare.placeholder((640, 480)).getdata())


def test_the_empty_screen_can_be_your_own_picture(tmp_path):
    mine = tmp_path / "mine.jpg"
    _photo(1600, 1200, (12, 200, 60)).save(mine)
    shown = prepare.no_files_screen((640, 480), str(mine))
    assert shown.size == (640, 480)
    assert shown.getpixel((320, 240))[1] > 150            # the green is what is on screen


def test_the_empty_screen_letterboxes_rather_than_crops(tmp_path):
    """Whatever you point it at, the whole of it is visible."""
    mine = tmp_path / "tall.jpg"
    _photo(600, 1200, (200, 30, 30)).save(mine)
    shown = prepare.no_files_screen((640, 480), str(mine))
    assert shown.getpixel((5, 240)) == (0, 0, 0)          # black bar, not a cropped photo
    assert shown.getpixel((320, 240))[0] > 150


def test_the_empty_screen_can_be_turned_off():
    drawn = prepare.placeholder((640, 480), "No pictures yet", "Looking in ~/Pictures")
    off = prepare.no_files_screen((640, 480), "none",
                                  message="No pictures yet", subtitle="Looking in ~/Pictures")
    assert list(off.getdata()) == list(drawn.getdata())


def test_an_unreadable_empty_screen_falls_back_to_the_drawing(tmp_path):
    """A typo in the path must not leave the frame black."""
    shown = prepare.no_files_screen((640, 480), str(tmp_path / "nope.jpg"))
    drawn = prepare.placeholder((640, 480), "No pictures yet")
    assert list(shown.getdata()) == list(drawn.getdata())


def test_prepare_records_how_the_picture_was_laid_out(tmp_path):
    """So the frame can answer "why was that one cropped?" out loud."""
    path = tmp_path / "p.jpg"
    _photo(1600, 900).save(path)
    meta = PhotoMeta(path=str(path), width=1600, height=900)
    for fit in ("cover", "contain", "blur", "mat"):
        out = prepare.prepare([meta], (1280, 720), prepare.PrepareOptions(fit=fit))
        assert out.info["picframe3_fit"] == fit

    # auto on a 16:9 picture and a 16:9 panel: nothing to mat, nothing to crop.
    out = prepare.prepare([meta], (1280, 720), prepare.PrepareOptions(fit="auto"))
    assert out.info["picframe3_fit"] == "cover"

    # auto on a portrait picture: matted rather than cropped.
    portrait = tmp_path / "q.jpg"
    _photo(900, 1200).save(portrait)
    out = prepare.prepare([PhotoMeta(path=str(portrait), width=900, height=1200)],
                          (1280, 720), prepare.PrepareOptions(fit="auto"))
    assert out.info["picframe3_fit"] == "mat"


# -- choosing between treatments -------------------------------------------

def test_auto_picks_only_from_the_allowed_fits(tmp_path):
    """Untick "cover" in the settings and nothing is ever cropped."""
    path = tmp_path / "p.jpg"
    _photo(900, 1200).save(path)
    meta = PhotoMeta(path=str(path), width=900, height=1200)
    for allowed in (["mat"], ["blur"], ["contain"], ["mat", "blur"]):
        out = prepare.prepare([meta], (1280, 720),
                              prepare.PrepareOptions(fit="auto",
                                                     fit_choices=tuple(allowed)))
        assert out.info["picframe3_fit"] in allowed, allowed


def test_a_picture_that_already_fits_is_never_given_a_mat(tmp_path):
    """Whatever is ticked: filling the screen crops nothing here."""
    path = tmp_path / "wide.jpg"
    _photo(1600, 900).save(path)
    meta = PhotoMeta(path=str(path), width=1600, height=900)
    out = prepare.prepare([meta], (1280, 720),
                          prepare.PrepareOptions(fit="auto", fit_choices=("mat",)))
    assert out.info["picframe3_fit"] == "cover"


def test_the_same_photograph_always_gets_the_same_treatment():
    """A frame that matted a picture yesterday and blurred it today reads as a
    fault, so the choice is seeded from the file path, not left to chance."""
    picks = {prepare.pick_auto_fit(("mat", "blur", "contain"), "/photos/a.jpg")
             for _ in range(20)}
    assert len(picks) == 1
    spread = {prepare.pick_auto_fit(("mat", "blur", "contain"), f"/photos/{i}.jpg")
              for i in range(40)}
    assert len(spread) > 1, "every picture got the same treatment"


def test_an_empty_or_nonsense_list_falls_back_to_a_mat():
    assert prepare.pick_auto_fit((), "x") == "mat"
    assert prepare.pick_auto_fit(("nonsense",), "x") == "mat"
