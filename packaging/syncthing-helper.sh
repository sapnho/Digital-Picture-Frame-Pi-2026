#!/usr/bin/env bash
#
# The three things about Syncthing that need root, and nothing else.
#
#   syncthing-helper on  <user>    install the package, run it for <user> at boot
#   syncthing-helper off <user>    stop it and leave it stopped
#
# Run by picframe3-syncthing-on@<user>.service and
# picframe3-syncthing-off@<user>.service, which are the only two units the
# frame's polkit rule lets the frame start.  The frame therefore cannot ask
# this script for anything but "on" or "off" for its own user -- everything
# else about Syncthing (the folder, the pairing, the web interface's address)
# is done over Syncthing's own API as the ordinary user, where it belongs.
#
# Installed by `picframe3 setup` as /usr/local/lib/picframe3/syncthing-helper.

set -euo pipefail

ACTION="${1:-}"
USER_NAME="${2:-}"

if [ -z "$ACTION" ] || [ -z "$USER_NAME" ]; then
  echo "usage: syncthing-helper on|off <user>" >&2
  exit 2
fi
if ! id "$USER_NAME" >/dev/null 2>&1; then
  echo "no such user: $USER_NAME" >&2
  exit 2
fi

UNIT="syncthing@${USER_NAME}.service"
DROPIN_DIR=/etc/systemd/system/syncthing@.service.d

case "$ACTION" in
  on)
    if ! command -v syncthing >/dev/null; then
      echo "installing syncthing…"
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq || true
      apt-get install -y -q --no-install-recommends syncthing
    fi

    # Syncthing's first run otherwise creates ~/Sync and starts keeping it in
    # step with nothing, which on a picture frame is a folder nobody asked
    # for that looks like part of the library.
    mkdir -p "$DROPIN_DIR"
    cat > "$DROPIN_DIR/picframe3.conf" <<'CONF'
# Installed by picframe3. Syncthing's default folder is not wanted on a
# picture frame -- the frame names the folder it wants itself.
[Service]
Environment=STNODEFAULTFOLDER=1
CONF
    systemctl daemon-reload
    systemctl enable --now "$UNIT"
    ;;

  off)
    systemctl disable --now "$UNIT" || true
    ;;

  *)
    echo "unknown action: $ACTION" >&2
    exit 2
    ;;
esac
