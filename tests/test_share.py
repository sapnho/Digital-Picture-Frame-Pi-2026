"""The Samba share config — the part that decides whether you can drop a
photograph on the frame or merely look at the folder."""

import pytest

from picframe3.wizard import SAMBA_BEGIN, SAMBA_END, share_config


def lines(**kw):
    return share_config("Pictures", "/home/pi/Pictures", "pi", **kw).splitlines()


def test_markers_wrap_the_block():
    """The block is rewritten in place on every run, so it must be findable."""
    text = share_config("Pictures", "/home/pi/Pictures", "pi", open_share=True)
    assert text.startswith(SAMBA_BEGIN)
    assert SAMBA_END in text


@pytest.mark.parametrize("open_share", [True, False])
def test_the_share_is_always_writable(open_share):
    assert "   read only = no" in lines(open_share=open_share)


def test_open_share_needs_no_password():
    got = lines(open_share=True)
    assert "   guest ok = yes" in got
    assert "   guest only = yes" in got          # never prompt, never fail
    assert "   map to guest = bad user" in got   # accept the anonymous session
    assert not any("valid users" in ln for ln in got)


def test_private_share_switches_guest_off_rather_than_ignoring_it():
    """The regression: `guest ok` merely absent let macOS connect as Guest and
    silently mount read-only. Guest has to be refused so Finder asks."""
    got = lines(open_share=False)
    assert "   guest ok = no" in got
    assert "   map to guest = never" in got
    assert "   valid users = pi" in got


@pytest.mark.parametrize("open_share", [True, False])
def test_writes_land_as_the_frames_own_user(open_share):
    """Whoever connects, the files must be owned by the user the slideshow
    runs as, or the frame cannot read what was just dropped in."""
    got = lines(open_share=open_share)
    assert "   force user = pi" in got
    assert "   force group = pi" in got
    assert "   force create mode = 0664" in got
    assert "   force directory mode = 0775" in got


def test_macos_metadata_goes_into_xattrs_rather_than_beside_the_photograph():
    """The `._DSC1234.jpg` problem, solved where it starts.

    Vetoing `._*` (which this share used to do) hides the files and makes
    writing one fail; it does not stop macOS wanting somewhere to put a
    file's Finder metadata. The `fruit` layer gives it somewhere -- real
    extended attributes -- so the files are never created. Samba needs all
    three VFS modules, in this order, for that to work.
    """
    got = lines(open_share=True)
    assert "   vfs objects = catia fruit streams_xattr" in got
    assert "   fruit:metadata = stream" in got
    assert "   fruit:resource = xattr" in got


def test_an_appledouble_file_from_elsewhere_stays_visible():
    """A `._` file off a USB stick or an old backup should be an ordinary
    file, not an invisible one the owner cannot delete over the share."""
    got = lines(open_share=True)
    assert "   fruit:veto_appledouble = no" in got
    assert not any("._*" in ln for ln in got)


def test_finder_and_spotlight_droppings_are_still_kept_out():
    got = lines(open_share=True)
    veto = next(ln for ln in got if ln.strip().startswith("veto files"))
    assert ".DS_Store" in veto and ".Spotlight-V100" in veto
    assert "   delete veto files = yes" in got


@pytest.mark.parametrize("open_share", [True, False])
def test_the_distros_home_shares_are_switched_off(open_share):
    """Otherwise Finder shows a second, useless share beside the pictures.

    Debian's `[homes]` carries `browseable = no`, which hides the `[homes]`
    entry but *not* the per-user share it generates — that one inherits
    `browseable` from `[global]`. A Mac connecting as Guest is mapped to
    `nobody`, so Finder lists a "nobody" share pointing at `/nonexistent`.
    """
    got = lines(open_share=open_share)
    assert "[homes]" in got
    assert got[got.index("[homes]") + 1] == "   available = no"


def test_the_home_shares_are_switched_off_before_the_picture_share():
    """Section order decides which section a line lands in: `available = no`
    has to sit under `[homes]`, not leak into the share below it."""
    got = lines(open_share=True)
    assert got.index("[homes]") < got.index("[Pictures]")
    assert "   available = no" not in got[got.index("[Pictures]"):]


def test_share_name_and_path_are_honoured():
    text = share_config("Fotos", "/mnt/photos", "frame", open_share=True)
    assert "[Fotos]" in text
    assert "   path = /mnt/photos" in text
    assert "   force user = frame" in text


# --- password prompts -------------------------------------------------------
# Hidden input makes a failed paste indistinguishable from a successful one.
# These pin the escape hatches.

def test_a_received_password_is_returned_and_acknowledged(capsys):
    from unittest import mock

    from picframe3 import wizard

    with mock.patch("getpass.getpass", return_value="  hunter2-secret  "):
        assert wizard.ask_secret("Password") == "hunter2-secret"
    assert "14 characters received" in capsys.readouterr().out


def test_empty_hidden_reads_fall_back_to_a_visible_prompt():
    from unittest import mock

    from picframe3 import wizard

    with mock.patch("getpass.getpass", return_value=""), \
         mock.patch.object(wizard, "_read", return_value="typed instead"):
        assert wizard.ask_secret("Password") == "typed instead"


def test_an_unusable_getpass_does_not_strand_the_wizard():
    from unittest import mock

    from picframe3 import wizard

    with mock.patch("getpass.getpass", side_effect=OSError("no tty")), \
         mock.patch.object(wizard, "_read", return_value="fallback"):
        assert wizard.ask_secret("Password") == "fallback"


def test_nothing_anywhere_returns_empty_rather_than_raising():
    from unittest import mock

    from picframe3 import wizard

    with mock.patch("getpass.getpass", side_effect=EOFError), \
         mock.patch.object(wizard, "_read", side_effect=EOFError):
        assert wizard.ask_secret("Password") == ""
