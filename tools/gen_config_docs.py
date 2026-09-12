#!/usr/bin/env python3
"""Generate docs/CONFIG.md and config/picframe3.example.yaml from the code.

The prose comes from ``picframe3.uischema``, which is also what draws the
settings page -- so the reference, the page and the code cannot drift apart.
Keeping the reference generated means it cannot go stale.

The example configuration is generated for exactly the same reason. While it
was maintained by hand it drifted, quietly: sixteen settings that existed in
the code, were documented in CONFIG.md and were offered on the settings page
had no line in the file people are told to copy -- among them
``http.allow_delete``, ``input.keymap``, ``mqtt.topic_prefix`` and
``logging.journald``. A starting point that omits a setting is worse than no
starting point, because it reads as the complete list. CI runs this script and
fails on any diff, so the omission cannot happen again.
"""
import pathlib
import sys
import textwrap
from dataclasses import fields, is_dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from picframe3.config import Config  # noqa: E402
from picframe3.uischema import (  # noqa: E402
    ADVANCED,
    NOTES,
    SECRETS,
    needs_restart,
)
from picframe3.uischema import SECTION_PROSE as PROSE  # noqa: E402


def type_name(annotation) -> str:
    text = str(annotation).replace("typing.", "")
    text = text.replace("<class '", "").replace("'>", "")
    optional = False
    while text.startswith("Optional[") and text.endswith("]"):
        text, optional = text[len("Optional["):-1], True
    pretty = {"str": "string", "bool": "boolean", "int": "integer",
              "float": "number", "list[str]": "list of strings",
              "list[int]": "list of integers", "list[float]": "list of numbers",
              "dict": "mapping"}.get(text, text)
    return pretty + (" or null" if optional else "")


# --------------------------------------------------------------------------
# The example configuration
# --------------------------------------------------------------------------

#: Where an inline comment starts, and how wide a comment line may be. Fixed
#: numbers rather than "as wide as the longest key in this section", so that
#: adding one long key cannot reflow the whole file into a huge diff.
COMMENT_COLUMN = 26
COMMENT_WIDTH = 72

EXAMPLE_HEADER = """\
# picframe3 configuration — a complete, commented starting point
# ---------------------------------------------------------------------------
# Generated from the code by tools/gen_config_docs.py — do not edit by hand.
# Every setting picframe3 has appears below, at its default value, so this file
# is also the answer to "what can I set?".
#
# Every key is optional: anything you leave out uses the built-in default shown
# here, and an unknown key produces a warning in the log rather than a crash.
# Copy this to ~/.config/picframe3/config.yaml (or run `picframe3 config
# --init`, which writes the same defaults).
#
# Changes made from the web UI or MQTT apply immediately; press "Save to config
# file" there (or `picframe3 config --set key=value`) to make them survive a
# restart. Settings marked "(restart)" only take effect when the frame starts
# again.
#
# The same list with types and explanations is in docs/CONFIG.md.
"""


def comment_for(dotted: str) -> str:
    """One plain-text sentence about a setting, or "" if there is nothing to say."""
    parts = []
    note = NOTES.get(dotted, "")
    if note:
        # The notes are written for Markdown; a YAML comment wants the words.
        parts.append(note.replace("`", ""))
    if needs_restart(dotted):
        parts.append("(restart)")
    if dotted in ADVANCED:
        parts.append("(advanced)")
    return " ".join(parts)


def dump_value(name: str, value) -> list[str]:
    """One key and its default, as the YAML lines it occupies.

    Short collections are written inline -- ``[0.0, 0.0, 0.0, 1.0]`` reads as
    one value, which is what it is, while the block form spreads four numbers
    over four lines and pushes the explanation away from them. A collection
    too long for that is written as a block whose innermost lists stay inline,
    so ``input.keymap`` is one readable line per action rather than thirty.
    """
    import yaml

    def dump(data, flow):
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                              default_flow_style=flow, width=10 ** 6).rstrip("\n")

    if isinstance(value, (list, dict)):
        candidate = f"{name}: {dump(value, True)}"
        if "\n" not in candidate and len(candidate) <= 72:
            return ["  " + candidate]
        # flow=None keeps the outer structure as a block (the value here is
        # always a non-empty collection, so the mapping has a nested one) and
        # collapses the leaves.
        text = dump({name: value}, None)
    else:
        text = dump({name: value}, False)
    return ["  " + line if line else line for line in text.splitlines()]


def example_lines() -> list[str]:
    cfg = Config()
    out = EXAMPLE_HEADER.splitlines()
    for section in fields(Config):
        if section.name == "source_path":
            continue
        obj = getattr(cfg, section.name)
        if not is_dataclass(obj):
            continue
        out.append("")
        for line in textwrap.wrap(PROSE.get(section.name, ""), COMMENT_WIDTH):
            out.append(f"# {line}")
        out.append(f"{section.name}:")
        for f in fields(obj):
            dotted = f"{section.name}.{f.name}"
            # Secrets are shown empty. A generated file full of real-looking
            # passwords is a file people paste somewhere they should not.
            value = "" if dotted in SECRETS else getattr(obj, f.name)
            body = dump_value(f.name, value)
            comment = comment_for(dotted)
            if not comment:
                out.extend(body)
                continue
            wrapped = textwrap.wrap(comment, COMMENT_WIDTH)
            if len(wrapped) == 1 and len(body) == 1 and \
                    len(body[0]) < COMMENT_COLUMN:
                out.append(f"{body[0]:<{COMMENT_COLUMN}}# {wrapped[0]}")
            else:
                # Too long to sit beside the value, or the value itself is a
                # block: the explanation goes above, where it has room.
                out.extend(f"  # {line}" for line in wrapped)
                out.extend(body)
    return out


def write_example() -> pathlib.Path:
    target = pathlib.Path(__file__).resolve().parents[1] / "config" / "picframe3.example.yaml"
    text = "\n".join(example_lines()) + "\n"

    # Prove it before writing it: a starting point that does not parse, or that
    # does not round-trip to the defaults it claims to show, is worse than none.
    import yaml

    loaded = yaml.safe_load(text)
    reference = Config().as_dict()
    for section, values in loaded.items():
        for key, value in values.items():
            expected = reference[section][key]
            if f"{section}.{key}" in SECRETS:
                continue
            assert value == expected, f"{section}.{key}: {value!r} != {expected!r}"
    missing = {
        f"{s}.{f.name}"
        for s in reference
        for f in fields(getattr(Config(), s))
        if f.name not in loaded.get(s, {})
    }
    assert not missing, f"the example is missing: {sorted(missing)}"

    target.write_text(text)
    return target


# --------------------------------------------------------------------------
# The reference
# --------------------------------------------------------------------------

def main() -> None:
    out = [
        "# Configuration reference",
        "",
        "*Generated from the code by `tools/gen_config_docs.py` — do not edit by hand.*",
        "",
        "The file lives at `~/.config/picframe3/config.yaml`. Every key is optional:",
        "anything you leave out uses the default below, and an unknown key produces a",
        "warning in the log rather than a crash. A fully commented starting point is in",
        "[`config/picframe3.example.yaml`](../config/picframe3.example.yaml).",
        "",
        "Every setting here also appears in the **Settings** tab of the web interface —",
        "the page is generated from the same definitions as this file, so neither can",
        "fall behind the code. That tab opens on a handful of cards, one per job",
        "(*how fast pictures change*, *where the photographs come from*), each holding",
        "the few settings that job needs; **All settings** at the foot of it is the",
        "whole list below, grouped the same way. Settings marked *(advanced)* are",
        "behind the “Show advanced” switch there. Anything can also be set from MQTT,",
        "or with `picframe3 config --set slideshow.interval=90`.",
        "",
    ]
    cfg = Config()
    import typing

    for section in fields(Config):
        if section.name == "source_path":
            continue
        obj = getattr(cfg, section.name)
        if not is_dataclass(obj):
            continue
        out.append(f"## `{section.name}`")
        out.append("")
        out.append(PROSE.get(section.name, ""))
        out.append("")
        out.append("| key | type | default | |")
        out.append("|---|---|---|---|")
        hints = typing.get_type_hints(type(obj))
        for f in fields(obj):
            dotted = f"{section.name}.{f.name}"
            value = getattr(obj, f.name)
            default = "`" + repr(value).replace("|", "\\|") + "`" if value != "" else "*(empty)*"
            if dotted in SECRETS:
                default = "*(empty)*"
            note = NOTES.get(dotted, "")
            if needs_restart(dotted):
                note = (note + " " if note else "") + "*(takes effect on restart)*"
            if dotted in ADVANCED:
                note = (note + " " if note else "") + "*(advanced)*"
            out.append(
                f"| `{f.name}` | {type_name(hints.get(f.name, f.type))} | {default} | "
                f"{note.replace('|', chr(92) + '|')} |"
            )
        out.append("")

    target = pathlib.Path(__file__).resolve().parents[1] / "docs" / "CONFIG.md"
    target.write_text("\n".join(out) + "\n")
    print(f"wrote {target} ({len(out)} lines)")
    example = write_example()
    print(f"wrote {example}")


if __name__ == "__main__":
    main()
