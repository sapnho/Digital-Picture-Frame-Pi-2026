"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from . import __version__
from .config import DEFAULT_CONFIG_PATHS, Config

_log = logging.getLogger("picframe3")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def setup_logging(level: str = "INFO", file: str = "", journald: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    under_systemd = bool(os.environ.get("JOURNAL_STREAM") or os.environ.get("INVOCATION_ID"))
    if under_systemd and journald:
        # systemd already timestamps every line; repeating it wastes the journal.
        fmt = "%(name)s: %(message)s"
    else:
        fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    root.addHandler(stream)

    if file:
        from logging.handlers import RotatingFileHandler

        path = os.path.expanduser(file)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        rotating = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3)
        rotating.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(rotating)

    for noisy in ("PIL", "uvicorn", "uvicorn.error", "asyncio", "aiomqtt"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_run(args) -> int:
    from .app import PicFrame

    config = Config.load(args.config)
    if args.backend:
        config.display.backend = args.backend
    if args.folder:
        config.library.picture_folders = list(args.folder)
    if args.no_http:
        config.http.enabled = False
    if args.interval:
        config.slideshow.interval = args.interval
    setup_logging(args.log_level or config.logging.level,
                  config.logging.file, config.logging.journald)
    _log.info("picframe3 %s starting", __version__)

    frame = PicFrame(config)

    async def main() -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, frame.request_stop)
            except NotImplementedError:  # pragma: no cover
                pass
        return await frame.run()

    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        return 0
    except Exception:
        _log.exception("picframe3 stopped with an error")
        return 1


def cmd_scan(args) -> int:
    from .library.db import Library
    from .library.scanner import Scanner
    from .media.geocode import Geocoder

    config = Config.load(args.config)
    setup_logging(args.log_level or "INFO", "", False)
    library = Library(config.library.database)
    geocoder = None
    if config.geo.enabled:
        geocoder = Geocoder(os.path.expanduser(config.geo.cache),
                            contact=config.geo.contact, enabled=True,
                            key_order=config.geo.key_order,
                            suppress=config.geo.suppress,
                            language=config.geo.language)
    scanner = Scanner(
        library,
        args.folder or config.library.picture_folders,
        follow_links=config.library.follow_links,
        geocoder=geocoder,
        include_videos=config.library.include_videos,
        ignore_hidden=config.library.ignore_hidden,
        exclude=config.library.exclude,
    )

    def progress(n: int, path: str) -> None:
        print(f"\r  {n:>7} files… {os.path.basename(path)[:48]:<48}", end="", flush=True)

    result = scanner.scan(prune=not args.no_prune, progress=progress)
    print("\r" + " " * 72 + "\r", end="")
    print(result.summary())

    if geocoder is not None:
        if args.regeocode:
            cleared = library.clear_locations()
            print(f"  cleared {cleared} cached place names")
        missing = library.count_locations_missing()
        if missing:
            print(f"  resolving {missing} place names "
                  f"(about one a second, as Nominatim requires)…")
            done = 0

            def show(_n: int, place: str) -> None:
                print(f"\r    {done + _n:>6} · {place[:44]:<44}", end="", flush=True)

            while True:
                batch = scanner.backfill_locations(limit=25, progress=show)
                done += batch
                if batch == 0:
                    break
            print("\r" + " " * 72 + "\r", end="")
            print(f"  {done} place names resolved, "
                  f"{library.count_locations_missing()} could not be identified")
    elif library.count_locations_missing():
        print(f"  {library.count_locations_missing()} photos have GPS coordinates; "
              f"set geo.enabled and geo.contact to turn them into place names")
    if result.errors:
        print(f"  {len(result.errors)} files could not be read")
        for path in result.errors[:10]:
            print(f"    {path}")
    stats = library.stats()
    print(f"  index now holds {stats['files']} files "
          f"({stats['videos']} videos) across {stats['folders']} folders")
    scanner.close()
    library.close()
    return 0


def cmd_doctor(args) -> int:
    """Check everything the frame needs, and say what to do about each gap."""
    setup_logging(args.log_level or "WARNING", "", False)
    ok = True
    print(f"picframe3 {__version__}  ·  python {sys.version.split()[0]}\n")

    def check(label: str, good: bool, detail: str = "", fix: str = "") -> bool:
        mark = "✓" if good else "✗"
        print(f" {mark} {label}" + (f"  {detail}" if detail else ""))
        if not good and fix:
            print(f"     → {fix}")
        return good

    # -- configuration
    config_path = args.config
    try:
        config = Config.load(config_path)
        loaded = config.source_path and os.path.exists(os.path.expanduser(config.source_path))
        check("configuration", True,
              config.source_path if loaded
              else f"built-in defaults (no file at {config.source_path})")
        if not loaded:
            print("     → optional: picframe3 config --init")
    except Exception as exc:
        check("configuration", False, str(exc),
              "create one with: picframe3 config --init")
        config = Config()
        ok = False

    # -- graphics
    try:
        from .gfx import drm

        outputs = list(drm.list_outputs())
        if outputs:
            for card, out in outputs:
                print(f"   {card}: {out.name} {out.width}x{out.height}"
                      f"@{out.mode.refresh_hz:.2f}Hz")
            check("DRM display", True, f"{len(outputs)} connected output(s)")
        else:
            ok &= check("DRM display", False, "no connected outputs",
                        "check the HDMI cable, or run with --backend headless to test")
    except Exception as exc:
        ok &= check("DRM display", False, str(exc),
                    "install libdrm2 and add this user to the 'video' group")

    for path in ("/dev/dri/card0", "/dev/dri/card1", "/dev/dri/renderD128"):
        if os.path.exists(path):
            readable = os.access(path, os.R_OK | os.W_OK)
            if not readable:
                ok &= check(f"access to {path}", False, "permission denied",
                            "sudo usermod -aG video,render $USER   (then log out and back in)")
            else:
                check(f"access to {path}", True)

    try:
        from .gfx import create_backend, gl

        backend = create_backend("headless", width=64, height=64)
        backend.make_current()
        version = gl.get_string(gl.VERSION)
        renderer = gl.get_string(gl.RENDERER)
        if gl.es_version_ok(version):
            check("OpenGL ES", True, f"{version} on {renderer}")
        else:
            major, minor = gl.REQUIRED_ES_VERSION
            ok &= check(
                "OpenGL ES", False, f"{version} on {renderer}",
                f"picframe3 needs OpenGL ES {major}.{minor}. The Raspberry Pi 4, "
                f"400 and 5 provide it; the Pi 2, 3 and Zero 2 W do not.",
            )
        backend.close()
    except Exception as exc:
        ok &= check("OpenGL ES", False, str(exc),
                    "install libgles2 and mesa drivers: sudo apt install libgles2 libgbm1")

    # -- media
    try:
        from PIL import Image, features

        bits = [f"Pillow {Image.__version__}"]
        for feature in ("libjpeg_turbo", "webp", "raqm"):
            if features.check(feature):
                bits.append(feature)
        check("image decoding", True, ", ".join(bits))
        if not features.check("raqm"):
            print("     → optional: sudo apt install libraqm0  (shaping for "
                  "Arabic/Hebrew/Indic captions)")
    except Exception as exc:
        ok &= check("image decoding", False, str(exc), "pip install Pillow")

    try:
        import pillow_heif  # noqa: F401

        check("HEIC/HEIF support", True)
    except ImportError:
        check("HEIC/HEIF support", False, "not installed",
              "pip install pillow-heif   (needed for iPhone photos)")

    from .media import video

    if video.available():
        check("video playback", True, "GStreamer")
    else:
        check("video playback", False, "GStreamer bindings missing",
              "sudo apt install python3-gi gstreamer1.0-plugins-good "
              "gstreamer1.0-libav gir1.2-gst-plugins-base-1.0")

    from .gfx import textstyle

    check("fonts", textstyle.font_available(), "",
          "sudo apt install fonts-dejavu-core")

    # -- library
    for folder in config.library.picture_folders:
        path = os.path.expanduser(folder)
        exists = os.path.isdir(path)
        ok &= check(f"picture folder {folder}", exists,
                    "" if exists else "missing",
                    f"mkdir -p {path}   (or change library.picture_folders)")
    try:
        from .library.db import Library

        library = Library(config.library.database)
        stats = library.stats()
        check("photo index", True,
              f"{stats['files']} files, {stats['db_bytes'] // 1024} KiB at {stats['db_path']}")
        if stats["files"] == 0:
            print("     → run: picframe3 scan")
        library.close()
    except Exception as exc:
        ok &= check("photo index", False, str(exc))

    # -- optional services
    for module, label, extra in (
        ("fastapi", "web interface", "pip install 'picframe3[web]'"),
        ("aiomqtt", "MQTT / Home Assistant", "pip install 'picframe3[mqtt]'"),
        ("evdev", "keyboard & touch input", "pip install 'picframe3[input]'"),
    ):
        try:
            __import__(module)
            check(label, True)
        except ImportError:
            check(label, False, "not installed", extra)

    # -- who am I
    import grp

    groups = {g.gr_name for g in grp.getgrall() if os.getlogin() in g.gr_mem} \
        if _can_getlogin() else set()
    for needed in ("video", "render", "input"):
        if groups and needed not in groups:
            print(f"   note: user is not in the '{needed}' group")

    print("\n" + ("All good." if ok else "Some checks failed — see the arrows above."))
    return 0 if ok else 1


def _can_getlogin() -> bool:
    try:
        os.getlogin()
        return True
    except OSError:
        return False


def cmd_config(args) -> int:
    setup_logging(args.log_level or "WARNING", "", False)
    if args.init:
        target = args.config or DEFAULT_CONFIG_PATHS[0]
        path = os.path.expanduser(target)
        if os.path.exists(path) and not args.force:
            print(f"{path} already exists; pass --force to overwrite")
            return 1
        config = Config()
        config.save(path)
        print(f"wrote a commented default configuration to {path}")
        return 0
    config = Config.load(args.config)
    if args.get:
        print(config.get(args.get))
        return 0
    if args.set:
        key, _, value = args.set.partition("=")
        config.set(key.strip(), _literal(value.strip()))
        config.save()
        print(f"{key.strip()} = {config.get(key.strip())}")
        return 0
    import yaml

    print(yaml.safe_dump(config.as_dict(), sort_keys=False, allow_unicode=True))
    return 0


def _literal(text: str):
    import yaml

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def cmd_demo(args) -> int:
    """Render sample frames offscreen -- useful before any hardware exists."""
    setup_logging(args.log_level or "INFO", "", False)
    from .demo import render_demo

    out = render_demo(args.output, args.folder, width=args.width, height=args.height,
                      transition=args.transition)
    print(f"wrote {out}")
    return 0


def cmd_setup(args) -> int:
    setup_logging(args.log_level or "WARNING", "", False)
    from .wizard import run as run_wizard

    return run_wizard(args.config, venv_bin=args.venv_bin, assume_yes=args.yes)


def cmd_uninstall(args) -> int:
    setup_logging(args.log_level or "WARNING", "", False)
    import getpass

    from .wizard import SERVICE_NAME, SERVICE_UNIT, run_root, say

    user = os.environ.get("SUDO_USER") or getpass.getuser()
    name = SERVICE_NAME.format(user=user)
    run_root(["systemctl", "disable", "--now", name])
    run_root(["rm", "-f", SERVICE_UNIT])
    run_root(["systemctl", "daemon-reload"])
    say(f"stopped and removed {name}")
    say("Your configuration, index and pictures were left alone.")
    say(f"  config  {Config.load(args.config).source_path}")
    return 0


def cmd_migrate(args) -> int:
    setup_logging(args.log_level or "WARNING", "", False)
    from .migrate import migrate

    target = args.output or (args.config or DEFAULT_CONFIG_PATHS[0])
    target = os.path.expanduser(target)
    if os.path.exists(target) and not args.force:
        print(f"{target} already exists; pass --force to overwrite")
        return 1
    config, notes = migrate(args.source, new_path=target)
    print(f"wrote {target}\n")
    print("Carried over:")
    print(f"  pictures     {', '.join(config.library.picture_folders)}")
    print(f"  interval     {config.slideshow.interval:g}s"
          f"  (transition {config.slideshow.transition}, "
          f"{config.slideshow.transition_time:g}s)")
    print(f"  fit          {config.viewer.fit}"
          + (f" / mat {config.viewer.mat_style}" if config.viewer.fit in ("auto", "mat") else ""))
    print(f"  captions     {', '.join(config.viewer.show_text) or 'none'}")
    print(f"  MQTT         {'on -> ' + config.mqtt.host if config.mqtt.enabled else 'off'}")
    print(f"  web          {'on, port ' + str(config.http.port) if config.http.enabled else 'off'}")
    if notes:
        print("\nNotes:")
        for note in notes:
            print(f"  · {note}")
    print("\nRun `picframe3 scan` to build the new index, then `picframe3 doctor`.")
    return 0


def cmd_transitions(args) -> int:
    from .gfx import transitions

    for name in transitions.names():
        print(name)
    return 0


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="picframe3",
        description="A digital picture frame for current Raspberry Pi OS.",
    )
    parser.add_argument("--version", action="version", version=f"picframe3 {__version__}")
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("-v", "--log-level", help="DEBUG, INFO, WARNING, ERROR")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="start the frame (default)")
    run.add_argument("--backend", choices=("auto", "kms", "headless"))
    run.add_argument("--folder", action="append", help="override the picture folder")
    run.add_argument("--interval", type=float, help="seconds per picture")
    run.add_argument("--no-http", action="store_true", help="do not start the web interface")
    run.set_defaults(func=cmd_run)

    scan = sub.add_parser("scan", help="index the picture folders and exit")
    scan.add_argument("--folder", action="append")
    scan.add_argument("--no-prune", action="store_true",
                      help="keep index entries whose files have gone")
    scan.add_argument("--regeocode", action="store_true",
                      help="discard the place names already resolved and look them up again")
    scan.set_defaults(func=cmd_scan)

    doctor = sub.add_parser("doctor", help="check the installation and the hardware")
    doctor.set_defaults(func=cmd_doctor)

    conf = sub.add_parser("config", help="show or change settings")
    conf.add_argument("--init", action="store_true", help="write a default config file")
    conf.add_argument("--force", action="store_true")
    conf.add_argument("--get", metavar="KEY")
    conf.add_argument("--set", metavar="KEY=VALUE")
    conf.set_defaults(func=cmd_config)

    demo = sub.add_parser("demo", help="render sample frames to a PNG, offscreen")
    demo.add_argument("-o", "--output", default="picframe3-demo.png")
    demo.add_argument("--folder", help="folder of real photos to use")
    demo.add_argument("--width", type=int, default=960)
    demo.add_argument("--height", type=int, default=540)
    demo.add_argument("--transition", default="fade")
    demo.set_defaults(func=cmd_demo)

    wiz = sub.add_parser("setup", help="interactive first-run setup")
    wiz.add_argument("--yes", action="store_true",
                     help="take every default without asking")
    wiz.add_argument("--venv-bin", help="bin/ directory of the virtual environment, "
                                        "used in the systemd unit")
    wiz.set_defaults(func=cmd_setup)

    sub.add_parser("uninstall", help="stop and remove the systemd service") \
        .set_defaults(func=cmd_uninstall)

    mig = sub.add_parser("migrate", help="convert a picframe configuration.yaml")
    mig.add_argument("source", help="path to the old configuration.yaml")
    mig.add_argument("-o", "--output", help="where to write the new config")
    mig.add_argument("--force", action="store_true")
    mig.set_defaults(func=cmd_migrate)

    sub.add_parser("transitions", help="list the available transitions") \
        .set_defaults(func=cmd_transitions)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args((argv or []) + ["run"])
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
