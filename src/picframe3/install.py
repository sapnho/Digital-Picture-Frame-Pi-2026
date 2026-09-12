"""Where this frame came from, and how to add a piece of it.

Every optional feature -- the web interface, MQTT, keyboard input, GPIO
buttons, HEIC -- fails with the same shape of message: "this needs X, install
it like so".  The command in that message has to be one the owner can paste,
and for a long time it was not: `pip install 'picframe3[web]'` names a package
that has never been published, and a bare `pip` on a Pi belongs to the system
Python rather than to the virtual environment the frame actually runs in.

One definition, used by the CLI's doctor and by every module that reports a
missing extra, so there is no second place for it to go stale.
"""

from __future__ import annotations

import os
import sys

#: picframe3 is not on PyPI, so `pip install picframe3` cannot work.  The
#: repository is what the installer uses and what these hints have to name.
SOURCE_URL = "git+https://github.com/sapnho/Digital-Picture-Frame-Pi-2026"


def pip_command() -> str:
    """The pip belonging to the interpreter this frame is running on.

    Naming a bare `pip` is a trap on a Pi: the frame lives in its own virtual
    environment under ~/.local/share/picframe3/venv, so a `pip` found on PATH
    installs into somewhere the frame will never look, and the missing module
    stays missing after a command that reported success.
    """
    candidate = os.path.join(os.path.dirname(sys.executable), "pip")
    return candidate if os.path.exists(candidate) else f"{sys.executable} -m pip"


def install_hint(extra: str) -> str:
    """A command the owner can paste that installs `extra` where it is needed."""
    return f'{pip_command()} install "{SOURCE_URL}#egg=picframe3[{extra}]"'
