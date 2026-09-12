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


def test_mat_colours_come_from_the_image():
    warm = mat.auto_colors(_photo(200, 200, (200, 80, 40)))[0]
    cool = mat.auto_colors(_photo(200, 200, (40, 80, 200)))[0]
    assert warm != cool
    assert all(c > 150 for c in warm), "outer mat should be a pale board"


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
