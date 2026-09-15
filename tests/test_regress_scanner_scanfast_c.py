"""Regressions for the C directory reader `disktide.scanner._scanfast`.

The two readers are meant to be one function, which includes the state the
caller's descriptor is in when it comes back: `os.scandir(fd)` rewinds the
directory before it lets go of it, so a descriptor can be read again.
"""

from __future__ import annotations

import os

import pytest

from disktide.scanner import _scanfast_py, accel


BACKENDS = ("native", "python")

_NATIVE_MISSING = (
    "disktide.scanner._scanfast is not built for this interpreter; "
    "the pure-Python fallback is the only reader here"
)


def reader(backend: str):
    """The `scan_dir` for a backend name, skipping when it does not exist."""
    if backend == "python":
        return _scanfast_py.scan_dir
    if not accel.NATIVE_AVAILABLE:
        pytest.skip(_NATIVE_MISSING)
    return accel.native_scan_dir


@pytest.fixture
def three_entries(tmp_path):
    """A directory with a couple of files and a subdirectory in it."""
    root = tmp_path / "twice"
    root.mkdir()
    (root / "a.bin").write_bytes(b"a" * 4)
    (root / "b.bin").write_bytes(b"b" * 4)
    (root / "c").mkdir()
    return root


@pytest.mark.parametrize("second", BACKENDS)
@pytest.mark.parametrize("first", BACKENDS)
def test_a_descriptor_can_be_read_twice(first, second, three_entries):
    """Whichever reader ran, the descriptor comes back re-readable.

    The native reader used to hand it back at the end of the directory --
    `dup` shares the file offset -- so a second read of the same descriptor,
    by either backend, saw an empty directory.
    """
    read_first, read_second = reader(first), reader(second)
    handle = os.open(str(three_entries), os.O_RDONLY | os.O_DIRECTORY)
    try:
        once = sorted(read_first(handle))
        first_offset = os.lseek(handle, 0, os.SEEK_CUR)
        twice = sorted(read_second(handle))
        second_offset = os.lseek(handle, 0, os.SEEK_CUR)
    finally:
        os.close(handle)

    assert [name for name, *_ in once] == ["a.bin", "b.bin", "c"]
    assert twice == once
    assert (first_offset, second_offset) == (0, 0)
