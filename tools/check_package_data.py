#!/usr/bin/env python3
"""Assert that everything the frame reads at runtime is inside the package.

A missing data file is the nastiest kind of packaging bug, because it never
shows up in development: an editable install and a source checkout both find
``src/picframe3/web/app.js`` on disk whatever the build backend was told to
include, so the tests pass, the demo renders, and the first person to discover
that the web interface is a blank page is the owner of a frame on a wall.

So this runs against a *non-editable* install in a clean virtual environment,
which is the only arrangement that answers the question honestly. Run it as::

    python tools/check_package_data.py

from an interpreter that has picframe3 installed (not from the source tree).
"""
from __future__ import annotations

import sys
from importlib.resources import files

#: Read at runtime by the renderer, the wizard and the web server. Each one is
#: a file the frame opens by name and cannot do without.
REQUIRED = (
    "data/no_pictures.jpg",           # shown when the library is empty
    "data/mat_texture.jpg",           # paper grain on the mat board
    "data/picframe3.service",         # the unit `picframe3 setup` installs
    "data/50-picframe3-network.rules",
    "data/55-picframe3-power.rules",       # permission to power the Pi off
    "data/99-picframe3.rules",
    "data/60-picframe3-syncthing.rules",   # the Syncthing switch, as root
    "data/picframe3-syncthing-on@.service",
    "data/picframe3-syncthing-off@.service",
    "data/syncthing-helper.sh",
    "web/index.html",
    "web/app.js",
    "web/style.css",
)


def main() -> int:
    root = files("picframe3")
    missing = [name for name in REQUIRED if not (root / name).is_file()]

    # The fonts are listed by pattern rather than by name: the web interface
    # ships whichever subsets it needs, and pinning the names here would mean
    # this check has to be edited every time one is added.
    try:
        fonts = [entry.name for entry in (root / "web" / "fonts").iterdir()
                 if entry.name.endswith(".woff2")]
    except (FileNotFoundError, NotADirectoryError):
        fonts = []
    if not fonts:
        missing.append("web/fonts/*.woff2")

    if missing:
        print("missing from the installed package:", file=sys.stderr)
        for name in missing:
            print(f"  picframe3/{name}", file=sys.stderr)
        print("\nfix [tool.hatch.build] in pyproject.toml", file=sys.stderr)
        return 1

    print(f"package data complete: {len(REQUIRED)} files "
          f"and {len(fonts)} font file(s), from {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
