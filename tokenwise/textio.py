"""Reading and writing source files without disturbing their line endings or byte-order mark.

Models always see text with plain "\\n" line endings. When a file is written back, it gets the
line ending style (CRLF or LF) and BOM it had before, so on Windows a one-line change stays a
one-line change in git instead of rewriting every line.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from pathlib import Path

BOM = codecs.BOM_UTF8


class NotTextError(ValueError):
    pass


@dataclass(frozen=True)
class Style:
    newline: str = "\n"
    bom: bool = False

    def to_dict(self) -> dict:
        return {"newline": self.newline, "bom": self.bom}

    @classmethod
    def from_dict(cls, data: dict | None) -> Style:
        return cls(**data) if data else cls()


def detect_style(raw: bytes) -> Style:
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    return Style(newline="\r\n" if crlf > lf else "\n", bom=raw.startswith(BOM))


def decode(raw: bytes, name: str = "file") -> str:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise NotTextError(f"{name}: not UTF-8 text, tokenwise only edits UTF-8 files") from None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_source(path: Path, strict: bool = True) -> str:
    """The file's text with "\\n" line endings. strict=False replaces undecodable bytes."""
    raw = path.read_bytes()
    if strict:
        return decode(raw, path.name)
    return raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def read_style(path: Path) -> Style:
    return detect_style(path.read_bytes()) if path.exists() else Style()


def write_source(path: Path, text: str, style: Style) -> None:
    data = text.replace("\r\n", "\n")
    if style.newline != "\n":
        data = data.replace("\n", style.newline)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((BOM if style.bom else b"") + data.encode("utf-8"))
