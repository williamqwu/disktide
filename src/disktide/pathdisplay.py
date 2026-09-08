"""Fitting a filesystem path into a fixed number of terminal cells.

A path is read from the right. `/fs/project/PRJ0042/alice/project/datasets/raw`
is one of thousands under the same four-segment prefix, and on an HPC
filesystem that prefix is most of the string: cropping from the right --
which is what a plain slice and every `Text` overflow rule do -- keeps the
half that says nothing and drops the half that identifies the directory.

So the middle goes first, and the tail is kept whole for as long as it
fits. `relative_label` is the other half of the same idea: inside a view
that is already about one root, the root prefix is not information at all
and the root itself is `.`.
"""

from __future__ import annotations

from pathlib import PurePath

from disktide.glyphs import visible_width
from disktide.rendering import is_safe_rendering

__all__ = [
    "ELLIPSIS",
    "ellipsis",
    "elide_path",
    "elide_text",
    "relative_label",
    "split_path",
]

ELLIPSIS = "…"


def ellipsis() -> str:
    """The elision marker, ASCII in safe-rendering mode."""
    return "..." if is_safe_rendering() else ELLIPSIS


def split_path(path: str) -> tuple[str, ...]:
    """Path segments, with a leading separator kept as its own segment."""
    return PurePath(path).parts


def elide_path(path: str, width: int) -> str:
    """Fit `path` into `width` cells, dropping leading segments first.

    Keeps as much of the tail whole as fits, and only when even the last
    segment is too long crops inside it -- still from the left, so a long
    filename loses its beginning rather than its extension.

    A non-positive `width` means "no measurement available yet", and the
    path comes back untouched for the caller to re-fit after layout.
    """
    if width <= 0 or visible_width(path) <= width:
        return path
    mark = ellipsis()
    parts = split_path(path)
    for start in range(1, len(parts)):
        candidate = mark + "/" + "/".join(parts[start:])
        if visible_width(candidate) <= width:
            return candidate
    # Nothing whole fits: crop into the last segment from the left.
    tail = parts[-1] if parts else path
    room = width - visible_width(mark)
    if room <= 0:
        return mark[:width]
    return mark + tail[-room:]


def relative_label(path: str, root: str | None) -> str:
    """`path` as it reads inside a view that is already about `root`.

    The root itself is `.`; anything under it loses the shared prefix.
    Anything outside it is returned unchanged rather than dressed up with
    `../..`, which says less than the absolute path it replaced.
    """
    if not root:
        return path
    normalized_root = root.rstrip("/") or "/"
    if path == root or path == normalized_root:
        return "."
    prefix = normalized_root if normalized_root.endswith("/") else normalized_root + "/"
    if path.startswith(prefix):
        return path[len(prefix):] or "."
    return path


def elide_text(text: str, width: int) -> str:
    """Crop ordinary prose to `width` cells, marking the cut on the right.

    The mirror image of `elide_path`: a sentence is read from the left, so
    it is the end that goes.
    """
    if width <= 0 or visible_width(text) <= width:
        return text
    mark = ellipsis()
    room = width - visible_width(mark)
    if room <= 0:
        return mark[:width]
    return text[:room] + mark
