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

__all__ = ["display_text", "json_text"]


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
