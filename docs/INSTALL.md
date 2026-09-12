# Installing picframe3 on a Raspberry Pi

**Four steps, about twenty minutes, most of it downloads.**

There is no compositor to install, no X compatibility layer, no autostart file
to write, no console autologin to enable and no `.service` file to paste into
an editor. picframe3 renders through EGL straight onto DRM/KMS, so it owns the
screen and starts before anyone logs in.

---

## What you need

- Raspberry Pi 4, 400 or 5 *(see [which Raspberry Pi](#which-raspberry-pi))*
- A microSD card, 16 GB or larger
- A display on HDMI, or an official DSI panel
- A network connection for the first few minutes

---

## Step 1 — Write the card

Use **Raspberry Pi Imager**.

1. **Choose device:** your Pi model.
2. **Choose OS:** Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**.
   Lite is the right image. picframe3 draws to the screen itself, so a desktop
   would only take memory and get in the way.
3. Click the gear / **Edit settings**:
   - **hostname:** `frame` — the frame's page will then be at `http://frame.local:9000/`
   - **username and password** — this guide assumes `pi`
   - your **Wi-Fi** network and country
   - **Enable SSH** on the Services tab
4. Write the card, put it in the Pi, connect the display, power it on, wait two
   minutes.

---

## Step 2 — Install

```bash
ssh pi@frame.local
curl -fsSL https://raw.githubusercontent.com/sapnho/Digital-Picture-Frame-Pi-2026/main/packaging/install.sh | bash
```

*(Prefer to read it first? `curl -fsSL … -o install.sh && less install.sh && bash install.sh`.)*

The script works out where to get the source: from a source tree it is sitting
in (a clone, an unpacked release), or — as here, piped from the web with no
script on disk — by downloading the repository itself.

The installer fetches the libraries, creates a virtual environment, installs
picframe3, puts you in the `video`, `render` and `input` groups, tidies the
boot options so no console text appears over the picture, and then asks you a
short series of questions:

```
1. This machine            confirms the Pi model and the display it found
2. Where your pictures are
3. Getting photographs onto the frame   Syncthing, a file share, or both
4. Controlling it from your phone       the web interface and its port
5. Home Assistant                       optional MQTT
6. How it should look                   interval, transition, mats,
                                        Ken Burns, clock, night-time off
7. Place names                          optional GPS → place names
8. Starting automatically               the systemd service
```

Every question has a default in brackets — holding Return down is a valid way
to use it. Nothing is destructive, and `picframe3 setup` re-runs it any time.

**Then reboot**, so the group membership takes effect:

```bash
sudo reboot
```

<details>
<summary>Installing by hand instead</summary>

```bash
sudo apt update
sudo apt install -y --no-install-recommends \
  python3-venv python3-dev gcc \
  libegl1 libgles2 libgbm1 libdrm2 \
  fonts-dejavu-core libraqm0 \
  python3-gi gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-libav

sudo usermod -aG video,render,input "$USER"

python3 -m venv --system-site-packages ~/.local/share/picframe3/venv
~/.local/share/picframe3/venv/bin/pip install \
  "git+https://github.com/sapnho/Digital-Picture-Frame-Pi-2026#egg=picframe3[all]"
sudo ln -sfn ~/.local/share/picframe3/venv/bin/picframe3 /usr/local/bin/picframe3

picframe3 setup
sudo reboot
```

picframe3 is installed **from the repository**, not from PyPI — there is no
`picframe3` package there, and `pip install picframe3` answers 404.

`--system-site-packages` matters: it lets the virtual environment see the
apt-installed PyGObject, which is how GStreamer is reached. Building PyGObject
inside the venv needs a toolchain and takes a long time on a Pi.

`python3-dev` and `gcc` are needed because `evdev` — the keyboard and
touchscreen support — is published as source only and is compiled during the
install.

Do not skip `picframe3 setup`: besides writing the configuration it installs
the systemd unit, the udev rule that makes `/dev/input/event*` readable by the
`input` group, the polkit rule that lets the frame reconnect its own Wi-Fi,
and the two oneshot units that let the settings page switch Syncthing on and
off without the frame ever having any other way to run something as root.
</details>

---

## Step 3 — Add pictures and index them

Question 3 of the setup offered two ways of getting photographs onto the
frame, and you can have both.

**Syncthing** pairs the frame with your phone, your Mac, your PC or a NAS once
and then keeps a folder in step by itself — a photograph taken this afternoon
is on the wall this afternoon, wherever you took it. Nothing passes through
anybody else's server; the devices talk to each other directly. After the
setup, the frame prints its own device id and the address of Syncthing's page
on it:

```
http://frame.local:8384/
```

To pair, add that device id in Syncthing on the other machine (Actions → Show
ID gives you its id in return), or paste the other machine's id into the
frame's settings page under **Getting photographs onto the frame** — the same
card shows what Syncthing is doing, and can install it later if you said no
during the setup. Then accept the shared folder on the other side.

The frame's folder is **send & receive** by default, which means a photograph
you Remove on the frame is removed from the phone that sent it as well.
There are two safety nets under that: Syncthing keeps its own copy of anything
deleted for 30 days (`.stversions` inside the picture folder), and the frame's
own **Removed** tab can put a picture back. If you would rather the frame
never sent anything back at all, set **Which way photographs travel** to
*Receive only*.

**A file share** makes the frame appear in Finder or Explorer, so you can drag
a folder onto it while you are on the same network:

- **macOS:** Finder → Go → Connect to Server → `smb://frame.local/Pictures`
  The share is set up with Samba's `fruit` layer, so Finder's metadata goes
  into extended attributes rather than into a `._DSC1234.jpg` beside every
  photograph.
- **Windows:** Explorer → `\\frame\Pictures`

Or, with neither:

```bash
scp -r ~/Photos/Italy2025 pi@frame.local:~/Pictures/
```

Then:

```bash
picframe3 scan
```

A few thousand photographs take a minute or two the first time; after that
only new and changed files are read. You do not have to run this again — new
files are noticed within seconds — but it is the quickest way to get going.

---

## Step 4 — Start it

```bash
sudo systemctl start picframe3@pi
```

The picture appears within a second or two, and from now on the frame starts
by itself on power.

```bash
systemctl status picframe3@pi          # is it happy?
journalctl -u picframe3@pi -f          # what is it doing?
```

Open **`http://frame.local:9000/`** on your phone:

- **Now playing** — what is on screen and its metadata; previous / pause /
  next; brightness and interval
- **Library** — search, browse by folder, tap a thumbnail to jump to it
- **Settings** — everything worth changing live, applied immediately, with a
  button to write it to the config file

At the frame, a keyboard works (→ next, ← previous, `p` pause, `i` info,
`o` screen off, `c` clock) and a touchscreen understands swipes and taps.

---

## Coming from the Bookworm/Wayland picframe guide

If you already have a frame built the
[pi3d PictureFrame way](https://www.thedigitalpictureframe.com/), these are no
longer needed and can be removed:

| No longer needed | why |
|---|---|
| `labwc`, `wayfire` | there is no compositor; the frame is the only thing on the screen |
| `xwayland` | nothing X11 is involved |
| `libsdl2-dev` | SDL was pi3d's window layer |
| `wlr-randr` | screen power is DRM DPMS, handled internally |
| `~/.config/labwc/autostart`, `rc.xml` | nothing to autostart inside a session |
| `~/.config/systemd/user/picframe.service` | a system service starts at boot instead |
| console autologin (`raspi-config`) | no login is needed for the frame to appear |

Convert your settings first — it reports exactly what it carried over and what
it dropped:

```bash
picframe3 migrate ~/picframe_data/config/configuration.yaml
systemctl --user disable --now picframe        # the old one
picframe3 scan
sudo systemctl enable --now picframe3@pi
```

Note the **`--user`**: the pi3d picframe guide installs its service as a user
unit in `~/.config/systemd/user/picframe.service`. `sudo systemctl disable
picframe` therefore fails with "Unit picframe.service does not exist", which
reads like "it was already gone" — and it was not. The old frame then comes
back at the next login and the two fight over the screen.

Both cannot run at once: only one process can be DRM master.

---

## Making it look like a frame

Use the **Settings** tab, or edit `~/.config/picframe3/config.yaml` and
`sudo systemctl restart picframe3@pi`. A good starting point for a shelf:

```yaml
slideshow:
  interval: 120
  transition: random
  transition_time: 3
  kenburns: true
  portrait_pairs: true

viewer:
  fit: auto
  mat_style: single_bevel double_bevel float_shadow   # pick from these
  show_text: [title, date, location]
  text_seconds: 12

power:
  schedule:
    all: ["23:00-07:00"]      # screen off overnight
  dim_schedule:
    "20:00-23:00": 0.45       # dim in the evening
```

### Portrait orientation

Rotate at the kernel level, so the Pi reports a portrait mode and every layer
works in real pixels. Append to the single line in
`/boot/firmware/cmdline.txt`:

```
video=HDMI-A-1:1080x1920M@60,rotate=90
```

Reboot; `picframe3 doctor` should then show a 1080×1920 mode. (For an
upside-down mount, `display.rotate: 180` in the config is enough.)

### Home Assistant

Answer yes at step 5, or set `mqtt.enabled`, `mqtt.host` and credentials. The
frame appears as one device with a light (on/off and brightness), a pause
switch, next/previous/rescan/remove buttons, an interval number, transition and
order selectors, and sensors for the current picture and library size. No YAML
on the Home Assistant side.

---

## Keeping it updated

Re-run the installer — it upgrades in place and leaves your configuration
alone:

```bash
curl -fsSL https://raw.githubusercontent.com/sapnho/Digital-Picture-Frame-Pi-2026/main/packaging/install.sh | bash
sudo systemctl restart picframe3@pi
```

An update will not change the version of Pillow, FastAPI or numpy your frame
runs on across a major release: every dependency in `pyproject.toml` has an
upper bound, so patch and minor releases (where the security fixes are) arrive
by themselves, while a new major version waits for a release of picframe3 that
has been tested against it. A frame in a hallway should not be able to break
itself on a Tuesday evening.

To remove the service again (your pictures, config and index are kept):

```bash
picframe3 uninstall
```

That stops and removes the service, the `/usr/local/bin/picframe3` shim, the
udev rule, the polkit rules, the Samba share and the two units that switch
Syncthing on and off. Syncthing itself is left installed, along with its
folders and its pairings: it may well be keeping folders that have nothing to
do with the frame. Your configuration, index and
photographs are left exactly where they are; delete
`~/.local/share/picframe3/venv` if you also want the disk space back.

---

## Which Raspberry Pi

**Pi 4, 400 or 5.** picframe3 renders with OpenGL ES 3, which Mesa's `v3d`
driver provides on those boards.

The **Pi 2, 3 and Zero 2 W** have VideoCore IV, which stops at OpenGL ES 2.0.
They are not supported: `picframe3 setup` says so, `picframe3 doctor` fails the
OpenGL check, and starting the frame stops with a message naming the reason
rather than limping along at one frame a second.

---

## Troubleshooting

Run `picframe3 doctor` first — every line is a tick, or a cross with the exact
command that fixes it.

**Nothing on screen, the service keeps restarting**

```bash
journalctl -u picframe3@pi -n 50
```

*"could not become DRM master"* means something else owns the display: you are
on the desktop image, or an `ssh` session is running `picframe3 run` at the same
time as the service. Stop the other one. On a desktop image:
`sudo systemctl set-default multi-user.target && sudo reboot`.

**"permission denied" on /dev/dri/card0** — you have not rebooted (or logged
out and in) since the installer added the groups.

**Black screen, no errors** — the display is asleep. Check `power.schedule`,
and try `curl -XPOST localhost:9000/api/display_on`.

**Pictures look soft** — the source is smaller than the panel. picframe3
switches to a blur-fill rather than enlarging a small image more than 2.5×;
raise `viewer.upscale_limit` if you would rather it enlarged.

**iPhone photos are skipped** — install HEIC support:
`~/.local/share/picframe3/venv/bin/pip install pillow-heif`
(`picframe3 doctor` prints the same command with the path your frame is
actually running from.)

**Photographs have stopped arriving over Syncthing** — `picframe3 sync` says
where it stands: installed, running, which folder it keeps, who it is paired
with and whether anything is waiting to be accepted. `picframe3 sync on`
switches it back on, `picframe3 sync folder` re-creates the frame's folder if
it has gone missing.

**Videos do not play** — `picframe3 doctor` says whether GStreamer is visible.
The usual cause is a venv created without `--system-site-packages`.

**Console text over the picture** — add `consoleblank=0 logo.nologo
vt.global_cursor_default=0 quiet` to `/boot/firmware/cmdline.txt`. The
installer does this for you, keeping the previous file next to it as
`cmdline.txt.picframe3.bak`. It is a **single line** — that is how the
bootloader reads it — so the installer refuses to touch a `cmdline.txt` that
has somehow acquired more than one, and tells you instead.

**Keyboard or touchscreen does nothing** — `/dev/input/event*` is readable
only by root until `picframe3 setup` installs
`/etc/udev/rules.d/99-picframe3.rules`; being in the `input` group is not
enough on its own, because Raspberry Pi OS Lite runs no seat manager.
`picframe3 doctor` opens a device node and says so. Run `picframe3 setup`, then
reboot.

**Place names never appear** — they need `geo.enabled` *and* `geo.contact`
(an email address, which OpenStreetMap's terms require). They fill in a few at
a time while the frame runs; `picframe3 scan` resolves the whole library at
once.
