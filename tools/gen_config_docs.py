#!/usr/bin/env python3
"""Generate docs/CONFIG.md from the config dataclasses.

The prose comes from ``picframe3.uischema``, which is also what draws the
settings page -- so the reference, the page and the code cannot drift apart.
Keeping the reference generated means it cannot go stale.
"""
import pathlib
import sys
from dataclasses import fields, is_dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from picframe3.config import Config  # noqa: E402
from picframe3.uischema import (  # noqa: E402  # noqa: E402
    ADVANCED,
    LIVE_KEYS,
    LIVE_SECTIONS,
    NOTES,
    SECRETS,
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
        "fall behind the code. Settings marked *(advanced)* are behind the “Show",
        "advanced” switch there. Anything can also be set from MQTT, or with",
        "`picframe3 config --set slideshow.interval=90`.",
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
            live = section.name in LIVE_SECTIONS or dotted in LIVE_KEYS
            note = NOTES.get(dotted, "")
            if not live:
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


if __name__ == "__main__":
    main()
