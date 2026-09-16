"""Which languages the frame can write dates in, and building the missing ones.

``viewer.locale`` switches month and day names with ``setlocale(LC_TIME)``,
and the C library can only switch to a locale that has been *generated* on
this system.  Raspberry Pi OS Lite generates one, usually ``en_GB.UTF-8``, so
``de_DE.UTF-8`` failed quietly and every caption stayed in English.

Generating a locale needs root, and the frame runs as an ordinary user with
``NoNewPrivileges``, so the building happens in ``picframe3 setup`` -- which
the installer runs on every install and every update.  It builds only what the
owner asked for -- the language in the configuration, and any named in
``PICFRAME_LOCALES`` -- and never asks a question.  Nothing is built up front.

What this deliberately does not touch is the system's own language: ``LANG``
and ``/etc/default/locale`` stay as they are, so the installer, apt, SSH and
the console go on speaking English.  Only the frame's dates change.

The settings page lists what is actually built (``locale -a``) as ready, and
the common languages that are not as "not built yet", with the command that
builds them -- so it never pretends a language will work before it does.
"""

from __future__ import annotations

import datetime as _dt
import functools
import os
import re
import subprocess
from dataclasses import dataclass, field

#: Offered in the settings menu even when not built, so the owner can pick one
#: without knowing its code.  Nothing here is built unless it is chosen.
COMMON_LOCALES: tuple[str, ...] = (
    "en_GB.UTF-8", "en_US.UTF-8", "de_DE.UTF-8", "fr_FR.UTF-8",
    "it_IT.UTF-8", "es_ES.UTF-8", "nl_NL.UTF-8", "pt_PT.UTF-8",
)

LOCALE_GEN = "/etc/locale.gen"
SUPPORTED = "/usr/share/i18n/SUPPORTED"

#: Further languages to build, space- or comma-separated:
#: ``PICFRAME_LOCALES="sv_SE da_DK" bash install.sh``.
ENV_EXTRA = "PICFRAME_LOCALES"

#: How a language calls itself, for the settings menu.  A code missing here is
#: shown as the code; the sample date beside it says the rest.
NATIVE_NAMES: dict[str, str] = {
    "en": "English", "de": "Deutsch", "fr": "Français", "it": "Italiano",
    "es": "Español", "pt": "Português", "nl": "Nederlands", "pl": "Polski",
    "sv": "Svenska", "da": "Dansk", "nb": "Norsk bokmål", "nn": "Norsk nynorsk",
    "fi": "Suomi", "cs": "Čeština", "sk": "Slovenčina", "sl": "Slovenščina",
    "hr": "Hrvatski", "hu": "Magyar", "ro": "Română", "el": "Ελληνικά",
    "tr": "Türkçe", "ru": "Русский", "uk": "Українська", "ja": "日本語",
    "zh": "中文", "ko": "한국어", "ca": "Català", "eu": "Euskara",
    "gl": "Galego", "is": "Íslenska", "et": "Eesti", "lv": "Latviešu",
    "lt": "Lietuvių", "ga": "Gaeilge", "cy": "Cymraeg", "lb": "Lëtzebuergesch",
}

_NAME = re.compile(r"^[a-z]{2,3}_[A-Z]{2}")


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

def key(name: str) -> str:
    """A comparison key: ``de_DE.UTF-8``, ``de_DE.utf8``, ``de-DE`` and
    ``de_DE`` are all the same language to the owner, and all ``de_de.utf8``
    here.  A name without an encoding means UTF-8, which is what the frame
    writes its captions in."""
    return canonical(name).lower().replace("utf-8", "utf8")


def canonical(name: str) -> str:
    """How a locale is written in the configuration: ``de_DE.UTF-8``."""
    text = (name or "").strip()
    base, at, modifier = text.partition("@")
    lang, dot, encoding = base.partition(".")
    lang = lang.replace("-", "_")
    if "_" in lang:
        head, _, tail = lang.partition("_")
        lang = f"{head.lower()}_{tail.upper()}"
    enc = encoding if dot else "UTF-8"
    if enc.lower().replace("-", "") == "utf8":
        enc = "UTF-8"
    return f"{lang}.{enc}" + (f"@{modifier}" if at else "")


def short(name: str) -> str:
    """``de_DE`` for ``de_DE.UTF-8`` -- how the installer lists them."""
    return canonical(name).replace(".UTF-8", "")


def wanted(configured: str = "", extra: str = "") -> list[str]:
    """The configured language and any named extras, once each -- nothing
    the owner did not ask for."""
    out: list[str] = []
    seen: set[str] = set()
    for name in (configured, *re.split(r"[\s,]+", extra or "")):
        if not name or not name.strip() or name.strip() in ("C", "POSIX"):
            continue
        if key(name) in seen:
            continue
        seen.add(key(name))
        out.append(canonical(name))
    return out


# --------------------------------------------------------------------------
# /etc/locale.gen
# --------------------------------------------------------------------------

@dataclass
class Plan:
    """What turning ``names`` on in a ``locale.gen`` amounts to."""

    text: str
    enabled: list[str] = field(default_factory=list)   # switched on by this plan
    already: list[str] = field(default_factory=list)   # were on before
    unknown: list[str] = field(default_factory=list)   # this system cannot build

    @property
    def changed(self) -> bool:
        return bool(self.enabled)

    @property
    def ready(self) -> list[str]:
        return [*self.already, *self.enabled]


def _entry(line: str) -> tuple[str, str] | None:
    """``("de_DE.UTF-8", "UTF-8")`` for a locale line, commented or not."""
    body = line.strip().lstrip("#").strip()
    parts = body.split()
    if len(parts) != 2 or not _NAME.match(parts[0]):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]):
        return None
    return parts[0], parts[1]


def _entry_key(entry: tuple[str, str]) -> str:
    """The key of a ``locale.gen`` line.  ``de_DE ISO-8859-1`` names its
    encoding in the second column, not in the name -- read without it, it
    would pass for ``de_DE.UTF-8`` and be switched on in its place."""
    name, charset = entry
    base, at, modifier = name.partition("@")
    if "." not in base:
        name = f"{base}.{charset}" + (f"@{modifier}" if at else "")
    return key(name)


def plan(locale_gen: str, supported: str, names: list[str]) -> Plan:
    """Uncomment ``names`` in ``locale_gen``, adding a line where it has none.

    ``supported`` is ``/usr/share/i18n/SUPPORTED``, the list of everything this
    system can build; a name it does not list is reported, never written, so a
    typo in the configuration cannot break ``locale-gen`` for every other
    language.  When the file is missing the ``locale.gen`` lines are the only
    reference.
    """
    lines = locale_gen.splitlines()
    by_key: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        entry = _entry(line)
        if entry:
            by_key.setdefault(_entry_key(entry), []).append(index)

    known: dict[str, str] = {}
    for line in supported.splitlines():
        entry = _entry(line)
        if entry and not line.lstrip().startswith("#"):
            known.setdefault(_entry_key(entry), f"{entry[0]} {entry[1]}")

    result = Plan(text=locale_gen)
    appended: list[str] = []
    for name in names:
        k = key(name)
        indexes = by_key.get(k, [])
        if any(not lines[i].lstrip().startswith("#") for i in indexes):
            result.already.append(canonical(name))
        elif indexes:
            first = indexes[0]
            lines[first] = lines[first].strip().lstrip("#").strip()
            result.enabled.append(canonical(name))
        elif k in known:
            appended.append(known[k])
            result.enabled.append(canonical(name))
        else:
            result.unknown.append(name)

    if result.changed:
        text = "\n".join(lines)
        if appended:
            text = text.rstrip("\n") + "\n" + "\n".join(appended)
        result.text = text + "\n"
    return result


# --------------------------------------------------------------------------
# What is built
# --------------------------------------------------------------------------

def available(run=subprocess.run) -> list[str]:
    """The languages this system has built, as ``de_DE.UTF-8``.

    ``locale -a`` needs no root.  Only UTF-8 locales are listed -- the frame
    writes its captions in UTF-8, and an ISO-8859 locale would put the wrong
    bytes into "März".
    """
    try:
        result = run(["locale", "-a"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in (result.stdout or "").splitlines():
        name = line.strip()
        if not _NAME.match(name) or not key(name).split("@")[0].endswith(".utf8"):
            continue
        if key(name) in seen:
            continue
        seen.add(key(name))
        out.append(canonical(name))
    return sorted(out)


def is_built(name: str, built: list[str]) -> bool:
    return key(name) in {key(b) for b in built}


@functools.lru_cache(maxsize=256)
def _sample(name: str, date_format: str, day: str) -> str:
    """``16. September 2026`` in ``name``, without touching this process.

    ``setlocale`` is process-wide, and the running frame formats a caption on
    another thread while the settings page asks for this -- so the sample is
    written by ``date`` in a child process with its own ``LC_ALL``.
    """
    env = {**os.environ, "LC_ALL": name}
    try:
        result = subprocess.run(["date", "-d", day, f"+{date_format}"],
                                capture_output=True, text=True, env=env, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def label(name: str, date_format: str = "%-d %B %Y",
          today: _dt.date | None = None, *, sample: bool = True) -> str:
    """``Deutsch (de_DE) — 16. September 2026``."""
    lang = canonical(name).split("_", 1)[0]
    text = f"{NATIVE_NAMES.get(lang, lang)} ({short(name)})"
    if sample:
        day = (today or _dt.date.today()).isoformat()
        written = _sample(canonical(name), date_format or "%-d %B %Y", day)
        if written:
            text += f" — {written}"
    return text


def options(current: str, date_format: str = "%-d %B %Y",
            built: list[str] | None = None,
            today: _dt.date | None = None) -> list[dict[str, str]]:
    """The date-language menu: system default, every built language, then the
    common ones that are not built, marked as such.  The configured one is
    always listed -- a menu shows its first entry for a value it does not
    have, which would silently swap the owner's language for the default."""
    built = available() if built is None else built
    items = [{"name": "", "label": "System default (English on most frames)"}]
    current = (current or "").strip()
    matched = False
    for name in built:
        value = name
        if current and key(current) == key(name):
            value = current            # keep the owner's spelling selected
            matched = True
        items.append({"name": value, "label": label(name, date_format, today)})
    not_built = [n for n in COMMON_LOCALES if not is_built(n, built)]
    if current and not matched and key(current) not in {key(n) for n in not_built}:
        not_built.insert(0, current)
    for name in not_built:
        value = current if current and key(current) == key(name) else name
        items.append({"name": value,
                      "label": f"{label(name, sample=False)} — not built yet, "
                               f"then run 'picframe3 setup --yes'"})
    return items
