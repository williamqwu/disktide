"""Which directory reader the scanner uses, decided once at import.

`scan_dir(fd, stat_dirs=False)` returns one tuple per entry --
``(name, d_type, errno, mode, size, blocks, dev, ino, nlink, mtime)`` -- and
comes either from the C extension `disktide.scanner._scanfast`, which reads
a whole directory and lstats every non-directory entry inside a single GIL
release, or from `disktide.scanner._scanfast_py`, which does the same work
with `os.scandir(fd)` one entry at a time.

The two produce the same tuples, so the scheduler has one entry loop, not
two, and a scan of a given tree produces the same nodes either way. What the
extension buys is thread scaling: a per-entry `DirEntry.stat` hands the GIL
back and takes it again, so on an 88,000-directory tree eight workers made
close to a million handoffs and finished slower than one worker did.

`DISKTIDE_ACCEL=0` (or `off`, `no`, `false`) forces the fallback, for
comparing the two on one machine and for getting out of the way of a
suspected extension bug without uninstalling anything. `disktide doctor`
prints which one is live.

When the extension is missing, `ACCEL_REASON` names
`tool/build_scanfast.py`. A released install has no such file and the hint
costs it a sentence; a development checkout has it and is by far the most
likely place to see this reason at all -- an editable install whose
extension was never built, or one whose `.c` has been edited since. Silently
running 2x slower is the failure this project has already had twice, once in
a wheel and once in the maintainer's own `uv sync`, so the reason line says
what to do about it.
"""

from __future__ import annotations

import os
from typing import Callable, Mapping

from disktide.scanner._scanfast_py import (
    DT_BLK,
    DT_CHR,
    DT_DIR,
    DT_FIFO,
    DT_LNK,
    DT_REG,
    DT_SOCK,
    DT_UNKNOWN,
)
from disktide.scanner._scanfast_py import scan_dir as python_scan_dir


__all__ = [
    "ACCEL_BACKEND",
    "ACCEL_REASON",
    "DT_BLK",
    "DT_CHR",
    "DT_DIR",
    "DT_FIFO",
    "DT_LNK",
    "DT_REG",
    "DT_SOCK",
    "DT_UNKNOWN",
    "NATIVE_AVAILABLE",
    "native_scan_dir",
    "python_scan_dir",
    "scan_dir",
]

_DISABLED_VALUES = frozenset({"0", "off", "no", "false"})


def accel_disabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether `DISKTIDE_ACCEL` asks for the pure-Python reader."""
    source = os.environ if environ is None else environ
    return (source.get("DISKTIDE_ACCEL") or "").strip().lower() in _DISABLED_VALUES


try:
    from disktide.scanner._scanfast import scan_dir as native_scan_dir
except ImportError as _exc:  # not built for this interpreter or platform
    native_scan_dir: Callable[..., list[tuple]] | None = None
    _NATIVE_IMPORT_ERROR: str | None = str(_exc)
else:
    _NATIVE_IMPORT_ERROR = None

#: Whether the extension is importable at all, whatever the env asks for.
NATIVE_AVAILABLE = native_scan_dir is not None

#: Appended to the "not built" reason. Kept short: `disktide doctor` prints
#: the whole reason on one line, and the tests pin that the phrase "not
#: built" survives.
_BUILD_HINT = "in a development checkout, `python tool/build_scanfast.py`"

if not NATIVE_AVAILABLE:
    scan_dir = python_scan_dir
    ACCEL_BACKEND = "python"
    ACCEL_REASON = (
        f"_scanfast is not built for this interpreter "
        f"({_NATIVE_IMPORT_ERROR}); {_BUILD_HINT}"
    )
elif accel_disabled():
    scan_dir = python_scan_dir
    ACCEL_BACKEND = "python"
    ACCEL_REASON = "DISKTIDE_ACCEL disables the native reader"
else:
    scan_dir = native_scan_dir
    ACCEL_BACKEND = "native"
    ACCEL_REASON = "batched directory reads (_scanfast)"
