"""Rendering filesystem names that are not valid text.

A POSIX filename is a bag of bytes with two forbidden values, `/` and NUL.
Nothing requires it to be UTF-8, and on any long-lived home directory a few
are not: an archive unpacked with a Latin-1 name, a file copied off a
filesystem with a different locale, a name a program wrote from raw bytes.

Python still hands those to us as `str`, because `os.listdir`/`os.scandir`
decode with the `surrogateescape` error handler: each undecodable byte
becomes one lone surrogate in the U+DC80..U+DCFF range, so `b"\\xff"` arrives
as `"\\udcff"`. That round-trips perfectly back to the same bytes through
`os.fsencode`, which is what makes it the right representation for a path
you are about to hand back to the kernel.

It is the wrong representation for anything else. A lone surrogate is not a
character; a strict UTF-8 stream refuses to encode it, so a single such name
anywhere in a scan used to take the whole text report down with
`UnicodeEncodeError: surrogates not allowed`. JSON has the same problem one
level up: `json.dumps` will happily write `"\\udcff"`, and the document that
comes out is invalid per RFC 8259 section 7 -- `jq` tolerates it, a Python
consumer doing `json.dumps(doc, ensure_ascii=False).encode()` does not.

The two helpers here are the two answers, and they differ on purpose. A text
report is read by a person who may want to know which bytes are actually on
disk, so `display_text` shows them. A JSON document is parsed by a program
that needs a well-formed string more than it needs the bytes, so `json_text`
substitutes the replacement character.
"""

from __future__ import annotations

import os

__all__ = ["display_text", "json_text", "load_text", "store_text"]


def display_text(value: str) -> str:
    """Render a name for a human-readable stream, escaping raw bytes.

    Valid names pass through unchanged. Undecodable bytes come out as their
    Python escapes -- `b"\\xff\\xfe"` renders as ``\\xff\\xfe`` -- which is
    both printable and precise about what is on disk.

    `os.fsencode` is what undoes the surrogateescape: it turns the lone
    surrogates back into the original bytes, and the strict-ish decode that
    follows is then the only thing deciding how to show them.
    """
    return os.fsencode(value).decode("utf-8", "backslashreplace")


def json_text(value: str) -> str:
    """Render a name for a JSON document, replacing raw bytes with U+FFFD.

    The encode/decode pair is the same undo-the-surrogateescape trick as
    `display_text`, with `replace` instead: every undecodable byte becomes
    one U+FFFD. The result is a string `json.dumps` can write and any
    consumer can encode, at the cost of no longer saying which byte it was
    -- the text report is where that question is answered.
    """
    return value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


# A filesystem name that came through `surrogateescape` cannot be handed to
# SQLite: the driver encodes bound text strictly, so one undecodable byte
# anywhere in a tree used to abort the whole snapshot with
# `UnicodeEncodeError: surrogates not allowed`. `store_text` escapes those
# bytes into text SQLite accepts and `load_text` puts them back, so a path
# still round-trips to the same bytes through `os.fsencode`.
#
# The marker is U+FFFF, a noncharacter: `surrogateescape` never produces it,
# so a name that needs escaping is recognisable by its first character alone
# and every other name is stored byte-for-byte as before -- which is what
# keeps databases written by older builds readable and their interned path
# rows matching.
_ESCAPE_MARK = "\uffff"


def store_text(value: str) -> str:
    """Escape a name for SQLite, reversibly.

    Names without undecodable bytes are returned unchanged, so nothing that
    an earlier build stored moves. Otherwise each lone surrogate becomes the
    marker followed by the two hex digits of the byte it stands for, a
    literal marker becomes the marker followed by a dot, and the whole
    string is prefixed with one more marker to say it is escaped.
    """
    if value.isascii():
        return value
    if _ESCAPE_MARK not in value and not any(
        "\udc80" <= char <= "\udcff" for char in value
    ):
        return value
    parts = [_ESCAPE_MARK]
    for char in value:
        if char == _ESCAPE_MARK:
            parts.append(_ESCAPE_MARK + ".")
        elif "\udc80" <= char <= "\udcff":
            parts.append(_ESCAPE_MARK + format(ord(char) - 0xDC00, "02x"))
        else:
            parts.append(char)
    return "".join(parts)


def load_text(value: str) -> str:
    """Undo `store_text`, leaving anything it did not write untouched.

    A value that does not start with the marker was stored verbatim, which
    covers every row written before this escape existed. A trailing or
    otherwise malformed escape is left as it is rather than raising: a
    truncated name is easier to look at than a failed query.
    """
    if value.isascii() or not value.startswith(_ESCAPE_MARK):
        return value
    parts: list[str] = []
    index = 1
    end = len(value)
    while index < end:
        char = value[index]
        if char != _ESCAPE_MARK:
            parts.append(char)
            index += 1
            continue
        if value[index + 1:index + 2] == ".":
            parts.append(_ESCAPE_MARK)
            index += 2
            continue
        digits = value[index + 1:index + 3]
        try:
            byte = int(digits, 16)
        except ValueError:
            return value
        if not 0x80 <= byte <= 0xFF:
            return value
        parts.append(chr(0xDC00 + byte))
        index += 3
    return "".join(parts)
