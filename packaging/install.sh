#!/usr/bin/env bash
#
# picframe3 installer for Raspberry Pi OS (Bookworm / Trixie, 64-bit).
#
#   curl -fsSL https://raw.githubusercontent.com/sapnho/Digital-Picture-Frame-Pi-2026/main/packaging/install.sh | bash
#
# Or, from an unpacked source tree:   bash packaging/install.sh
#
# Safe to re-run: it upgrades in place, never overwrites your configuration,
# and asks nothing it can work out for itself. Re-running is also how you
# update.
#
# Unattended:  PICFRAME_YES=1 bash install.sh    (takes every default)

set -euo pipefail

VENV="${PICFRAME_VENV:-$HOME/.local/share/picframe3/venv}"
CONFIG="${PICFRAME_CONFIG:-$HOME/.config/picframe3/config.yaml}"
SHIM="/usr/local/bin/picframe3"
RUN_USER="${SUDO_USER:-${USER:-$(id -un)}}"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
step()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '   \033[33m!\033[0m %s\n' "$*"; }
die()   { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Normally run as the ordinary user, escalating with sudo where needed. Running
# the whole thing as root also works (some people do), in which case sudo is
# simply not used.
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
  [ -z "${SUDO_USER:-}" ] && warn "running as root; the frame will run as root too"
elif command -v sudo >/dev/null; then
  SUDO="sudo"
else
  die "sudo is not installed and you are not root"
fi

REPO="${PICFRAME_REPO:-sapnho/Digital-Picture-Frame-Pi-2026}"
BRANCH="${PICFRAME_BRANCH:-main}"
ARCHIVE="${PICFRAME_ARCHIVE:-https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz}"

# Where does the source come from? Three cases, in order:
#
#   tree      this script sits inside an unpacked source tree (a clone, a
#             release tarball) -- install that
#   explicit  PICFRAME_SOURCE names a path or a pip requirement
#   download  everything else, which crucially includes `curl … | bash`:
#             piped from the web there IS no script on disk and no source
#             tree, so fetch the repository itself
#
# BASH_SOURCE must be checked for being a real FILE. Piped into bash it is
# unset, `dirname` of nothing is ".", and a naive check then probes the
# user's home directory for a pyproject.toml that has nothing to do with us.
HERE=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$HERE" ] && [ -f "$HERE/../pyproject.toml" ]; then
  MODE="tree"
  SOURCE="$(cd "$HERE/.." && pwd)"
  SOURCE_DESC="this source tree ($SOURCE)"
elif [ -n "${PICFRAME_SOURCE:-}" ]; then
  MODE="explicit"
  SOURCE="$PICFRAME_SOURCE"
  SOURCE_DESC="$SOURCE"
else
  MODE="download"
  SOURCE=""
  SOURCE_DESC="$REPO ($BRANCH)"
fi

WORKDIR=""
cleanup() { [ -n "$WORKDIR" ] && rm -rf "$WORKDIR"; }
trap cleanup EXIT

bold "picframe3 installer"
echo "   installing $SOURCE_DESC"
echo "   for user   $RUN_USER"

# ---------------------------------------------------------------- 1. packages
step "1/5  System packages"
# Deliberately short. There is no compositor, no X server and no SDL here:
# the frame renders through EGL onto DRM/KMS by itself.
PACKAGES=(
  python3-venv python3-dev
  libegl1 libgles2 libgbm1 libdrm2          # the graphics path
  fonts-dejavu-core libraqm0                # captions, incl. shaped scripts
  python3-gi gir1.2-glib-2.0                # GStreamer bindings…
  gir1.2-gst-plugins-base-1.0
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good
  gstreamer1.0-libav gstreamer1.0-alsa      # …and video playback
)
if command -v apt-get >/dev/null; then
  $SUDO apt-get update -qq
  if $SUDO apt-get install -y --no-install-recommends "${PACKAGES[@]}" >/dev/null 2>&1; then
    ok "${#PACKAGES[@]} packages present"
  else
    # Package names drift between Debian releases. Rather than fail the whole
    # install because one name changed, take them one at a time and say which
    # ones this release does not have.
    warn "installing one at a time to find out which name this release uses"
    MISSING=()
    for pkg in "${PACKAGES[@]}"; do
      $SUDO apt-get install -y --no-install-recommends "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
    done
    if [ ${#MISSING[@]} -eq 0 ]; then
      ok "${#PACKAGES[@]} packages present"
    else
      warn "not available here: ${MISSING[*]}"
      warn "picframe3 will still install; 'picframe3 doctor' will say what is missing"
    fi
  fi
else
  warn "no apt-get here; install the equivalents yourself"
fi

# ------------------------------------------------------------------ 2. groups
step "2/5  Permissions"
# video + render: the DRM device.  input: keyboard and touchscreen, which are
# read straight from evdev because there is no display server to do it for us.
$SUDO usermod -aG video,render,input "$RUN_USER" 2>/dev/null || true
ok "$RUN_USER is in video, render and input"

# -------------------------------------------------------------------- 3. venv
step "3/5  picframe3"
if [ "$MODE" = "download" ]; then
  WORKDIR="$(mktemp -d)"
  echo "   fetching $REPO…"
  if curl -fsSL "$ARCHIVE" 2>/dev/null | tar xz -C "$WORKDIR" 2>/dev/null && \
     [ -n "$(find "$WORKDIR" -maxdepth 2 -name pyproject.toml -print -quit)" ]; then
    SOURCE="$(dirname "$(find "$WORKDIR" -maxdepth 2 -name pyproject.toml -print -quit)")"
  elif command -v git >/dev/null && \
       git clone --depth 1 --branch "$BRANCH" "https://github.com/$REPO.git" \
         "$WORKDIR/src" >/dev/null 2>&1; then
    SOURCE="$WORKDIR/src"
  else
    die "could not download $REPO. Check the network, or download the source and run packaging/install.sh from inside it."
  fi
  ok "source downloaded"
fi

mkdir -p "$(dirname "$VENV")"
# --system-site-packages so the apt PyGObject (and so GStreamer) is visible.
# Building PyGObject inside a venv needs a toolchain and several minutes.
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/pip" install --upgrade pip wheel >/dev/null
if [ -d "$SOURCE" ]; then
  "$VENV/bin/pip" install --upgrade "$SOURCE[all]" >/dev/null
else
  "$VENV/bin/pip" install --upgrade "$SOURCE" >/dev/null
fi
$SUDO ln -sfn "$VENV/bin/picframe3" "$SHIM"
ok "$("$VENV/bin/picframe3" --version) installed, 'picframe3' on your PATH"

# ------------------------------------------------------------------- 4. setup
step "4/5  Setting it up"
if [ -n "${PICFRAME_YES:-}" ]; then
  "$SHIM" --config "$CONFIG" setup --yes --venv-bin "$VENV/bin"
else
  # The wizard handles the picture folder, the network share, the web
  # interface, Home Assistant, the look, and the systemd service.
  "$SHIM" --config "$CONFIG" setup --venv-bin "$VENV/bin"
fi

# ------------------------------------------------------------------- 5. check
step "5/5  Checking"
"$SHIM" --config "$CONFIG" doctor || true

echo
bold "Installed."
cat <<NEXT

   picframe3 scan                    index your pictures
   sudo systemctl start picframe3@$RUN_USER
   journalctl -u picframe3@$RUN_USER -f    watch what it is doing

   Re-run this installer any time to update.
   picframe3 setup       change the answers
   picframe3 uninstall   stop and remove the service (keeps your pictures)

NEXT
