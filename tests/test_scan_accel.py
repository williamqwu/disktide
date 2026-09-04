"""The two directory readers, held to the same tuples and the same trees.

`disktide.scanner.accel.scan_dir` is either the C extension `_scanfast` or
the pure-Python `_scanfast_py`, chosen once at import. The scheduler has one
entry loop and calls whichever it got, so everything the two disagree about
becomes a difference in a scanned tree -- which is why most of what is below
is a comparison rather than an assertion about a value.

Every backend-sensitive test is parametrised over both. Where the extension
was not built for the running interpreter -- a platform with no wheel, an
sdist install with no compiler -- the native half skips with that reason;
the fallback half always runs, because the fallback always exists.
"""

from __future__ import annotations

import errno
import os
import shutil
import socket
import stat
import threading

import pytest

from disktide.domain.policy import ScanPolicy
from disktide.scanner import _scanfast_py, accel
from disktide.scanner import scheduler as scheduler_module
from disktide.scanner.accel import DT_DIR, DT_LNK, DT_REG
from disktide.scanner.engine import ScanEngine
from disktide.scanner.scheduler import (
    DirectoryJob,
    open_scan_directory,
    scan_directory_once,
)


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


def read(backend: str, path, stat_dirs: bool = False) -> list[tuple]:
    """One directory, read through one backend, on a descriptor of its own.

    A fresh descriptor per call because both readers hand theirs to
    `fdopendir` through a `dup`, which shares the file offset: after either
    of them has run, the caller's descriptor is at the end of the directory.
    That is `os.scandir(fd)`'s behaviour too, and the scheduler opens a
    descriptor per directory and reads it once.
    """
    scan_dir = reader(backend)
    handle = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        return sorted(scan_dir(handle, stat_dirs))
    finally:
        os.close(handle)


@pytest.fixture
def every_type(tmp_path):
    """One directory holding one entry of every kind readdir can name."""
    root = tmp_path / "zoo"
    root.mkdir()
    (root / "regular.bin").write_bytes(b"x" * 40)
    (root / "subdir").mkdir()
    (root / "subdir" / "leaf.bin").write_bytes(b"y" * 8)
    os.symlink("regular.bin", root / "link-ok")
    os.symlink("nowhere-at-all", root / "link-broken")
    os.mkfifo(root / "fifo")
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(root / "socket"))
    server.close()
    # A name the filesystem accepts and UTF-8 does not: both readers have to
    # produce the same surrogate-escaped string for it.
    with open(os.path.join(os.fsencode(root), b"not-utf8-\xff\xfe"), "wb") as handle:
        handle.write(b"z" * 3)
    return root


# --- the two readers agree -------------------------------------------------


def test_both_readers_produce_the_same_tuples(every_type):
    if not accel.NATIVE_AVAILABLE:
        pytest.skip(_NATIVE_MISSING)
    native = read("native", every_type)
    python = read("python", every_type)
    assert native == python
    # ...and the fixture really did carry one of everything.
    kinds = {name: dtype for name, dtype, *_ in native}
    assert kinds["subdir"] == DT_DIR
    assert kinds["regular.bin"] == DT_REG
    assert kinds["link-ok"] == DT_LNK
    assert kinds["link-broken"] == DT_LNK
    assert kinds["fifo"] == _scanfast_py.DT_FIFO
    assert kinds["socket"] == _scanfast_py.DT_SOCK
    assert any(name.startswith("not-utf8-") for name, *_ in native)


def test_both_readers_agree_with_stat_dirs(every_type):
    if not accel.NATIVE_AVAILABLE:
        pytest.skip(_NATIVE_MISSING)
    assert read("native", every_type, True) == read("python", every_type, True)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_directory_is_not_statted_unless_asked(backend, every_type):
    lazy = {name: row for name, *row in read(backend, every_type)}
    eager = {name: row for name, *row in read(backend, every_type, True)}

    assert lazy["subdir"][2:] == [None] * 7
    assert eager["subdir"][2] is not None
    assert stat.S_ISDIR(eager["subdir"][2])
    # Everything that is not a directory is statted either way.
    assert lazy["regular.bin"] == eager["regular.bin"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_stat_fields_are_the_ones_os_stat_reports(backend, every_type):
    rows = {name: row for name, *row in read(backend, every_type)}
    _, _, mode, size, blocks, dev, ino, nlink, mtime = rows["regular.bin"]
    expected = os.lstat(every_type / "regular.bin")

    assert mode == expected.st_mode
    assert size == expected.st_size == 40
    assert blocks == expected.st_blocks
    assert (dev, ino, nlink) == (
        expected.st_dev, expected.st_ino, expected.st_nlink
    )
    # Bit for bit: the extension builds the float the way CPython does,
    # seconds plus nanoseconds scaled, and a difference here would move
    # every file's mtime in a scan.
    assert mtime == expected.st_mtime


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_broken_symlink_measures_the_link_not_the_target(backend, every_type):
    rows = {name: row for name, *row in read(backend, every_type)}
    dtype, err, mode, size, *_ = rows["link-broken"]

    assert dtype == DT_LNK
    assert err == 0
    assert stat.S_ISLNK(mode)
    assert size == len("nowhere-at-all")


# --- errors -----------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_an_entry_that_vanished_carries_its_errno(backend, tmp_path, monkeypatch):
    """The readdir listed it; the stat found it gone.

    Provoked where the readers differ most: the extension stats inside the
    same call, so the removal is stamped on by the same shim the scheduler
    tests use, and what is asserted here is that the *scheduler* reads the
    tuple as a vanished entry rather than a denial.
    """
    (tmp_path / "keep.bin").write_bytes(b"x" * 16)
    (tmp_path / "gone.bin").write_bytes(b"y" * 16)
    real = reader(backend)

    def scan_dir(fd, stat_dirs=False):
        rows = []
        for row in real(fd, stat_dirs):
            if row[0] == "gone.bin":
                os.unlink(tmp_path / "gone.bin")
                row = (row[0], row[1], errno.ENOENT) + (None,) * 7
            rows.append(row)
        return rows

    monkeypatch.setattr(scheduler_module, "scan_dir", scan_dir)
    result = scan_directory_once(
        DirectoryJob(str(tmp_path), 0, None),
        policy=ScanPolicy(),
        cancel_event=threading.Event(),
        root_device=None,
        excluded_mounts={},
    )

    assert result.direct_vanished == 1
    assert result.direct_inaccessible == 0
    assert result.node.error is None
    assert [child.name for child in result.node.children] == ["keep.bin"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_directory_removed_after_it_was_listed_is_vanished(
    backend, tmp_path, monkeypatch
):
    """No simulation for this half: a directory is never statted by the read.

    The parent lists `doomed`, the shim removes it before the scheduler can
    hand out a job for it, and the job opens a name that is genuinely gone.
    """
    (tmp_path / "doomed").mkdir()
    (tmp_path / "doomed" / "leaf.bin").write_bytes(b"x" * 8)
    (tmp_path / "kept").mkdir()
    real = reader(backend)
    removed = threading.Event()

    def scan_dir(fd, stat_dirs=False):
        rows = real(fd, stat_dirs)
        if not removed.is_set():
            shutil.rmtree(tmp_path / "doomed")
            removed.set()
        return rows

    monkeypatch.setattr(scheduler_module, "scan_dir", scan_dir)
    root = ScanEngine(workers=1, scan_path=str(tmp_path)).scan(str(tmp_path))
    doomed = root.find(str(tmp_path / "doomed"))

    assert doomed is not None
    assert doomed.vanished is True
    assert doomed.error is None
    assert root.vanished_count == 1
    assert root.inaccessible_count == 0


@pytest.mark.parametrize("backend", BACKENDS)
def test_entries_of_a_directory_that_cannot_be_traversed_are_denied(
    backend, tmp_path
):
    """EACCES on the entries, from a directory that is readable but not `x`.

    Mode 0o400 is the one arrangement where readdir succeeds and the stat
    that follows it does not: the names come back, every `fstatat` under
    them is refused, and the scheduler has to call that inaccessible rather
    than vanished.
    """
    if os.getuid() == 0:
        pytest.skip("root traverses a directory without +x anyway")
    denied = tmp_path / "no-exec"
    denied.mkdir()
    (denied / "secret.bin").write_bytes(b"x" * 8)
    os.chmod(denied, 0o400)
    try:
        rows = read(backend, denied)
    finally:
        os.chmod(denied, 0o755)

    assert rows == [("secret.bin", DT_REG, errno.EACCES) + (None,) * 7]


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_descriptor_that_is_not_a_directory_raises(backend, tmp_path):
    """Whatever the open failed to be, the reader raises rather than lies."""
    plain = tmp_path / "plain.bin"
    plain.write_bytes(b"x" * 8)
    handle = os.open(str(plain), os.O_RDONLY)
    try:
        with pytest.raises(OSError) as caught:
            reader(backend)(handle)
        assert caught.value.errno == errno.ENOTDIR
    finally:
        os.close(handle)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_closed_descriptor_raises_ebadf(backend, tmp_path):
    handle = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    os.close(handle)

    with pytest.raises(OSError) as caught:
        reader(backend)(handle)
    assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize("backend", BACKENDS)
def test_an_unreadable_directory_is_an_error_not_a_crash(backend, tmp_path):
    """The same thing seen from the scheduler: a node with an error on it."""
    if os.getuid() == 0:
        pytest.skip("root reads a mode 000 directory anyway")
    (tmp_path / "open").mkdir()
    (tmp_path / "open" / "leaf.bin").write_bytes(b"x" * 8)
    denied = tmp_path / "denied"
    denied.mkdir()
    (denied / "secret.bin").write_bytes(b"x" * 8)
    os.chmod(denied, 0o000)
    try:
        root = ScanEngine(workers=1, scan_path=str(tmp_path)).scan(str(tmp_path))
    finally:
        os.chmod(denied, 0o755)
    node = root.find(str(denied))

    assert node is not None
    assert node.error is not None
    assert "Permission denied" in node.error
    assert root.find(str(tmp_path / "open" / "leaf.bin")) is not None


# --- descriptors ------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_callers_descriptor_survives_the_read(backend, every_type):
    """Both readers `dup` before `fdopendir`, so the caller still owns its fd.

    The scheduler opens the directory, reads it, and closes the descriptor
    in a `finally`; a reader that closed it would turn that into EBADF on
    every directory.
    """
    handle = os.open(str(every_type), os.O_RDONLY | os.O_DIRECTORY)
    try:
        rows = reader(backend)(handle)
        assert rows
        # Still open, still this directory.
        assert os.fstat(handle).st_ino == os.stat(every_type).st_ino
        assert stat.S_ISDIR(os.fstat(handle).st_mode)
    finally:
        os.close(handle)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_directory_past_path_max_is_read_through_its_descriptor(
    backend, tmp_path
):
    """A path no syscall can name is still a descriptor either reader takes.

    `open_scan_directory` walks a path longer than PATH_MAX in chunks and
    hands back one descriptor; from there the reader never sees a pathname
    at all, so ENAMETOOLONG cannot reach it.
    """
    name = "d" * 200
    handle = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    parts = [str(tmp_path)]
    try:
        for _ in range(40):
            os.mkdir(name, dir_fd=handle)
            parts.append(name)
            nested = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=handle)
            os.close(handle)
            handle = nested
        with open(
            os.open("deep.bin", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=handle),
            "wb",
        ) as target:
            target.write(b"q" * 64)
    finally:
        os.close(handle)
    deep = "/".join(parts)
    assert len(deep) > 4096, "the fixture has to outgrow PATH_MAX"
    with pytest.raises(OSError) as too_long:
        os.open(deep, os.O_RDONLY | os.O_DIRECTORY)
    assert too_long.value.errno == errno.ENAMETOOLONG

    handle = open_scan_directory(deep, follow_symlink=False)
    try:
        rows = reader(backend)(handle)
        # The reference stat has to go through a descriptor too -- naming
        # the file is what does not fit.
        expected = os.lstat("deep.bin", dir_fd=handle)
    finally:
        os.close(handle)

    assert [(row[0], row[1], row[2], row[3], row[4]) for row in rows] == [
        ("deep.bin", DT_REG, 0, expected.st_mode, expected.st_size)
    ]


# --- the scheduler on top of them ------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_scheduler_builds_the_same_tree_either_way(
    backend, every_type, monkeypatch
):
    """What `tool/dump_tree.py` diffs, in the suite, at every commit."""
    monkeypatch.setattr(scheduler_module, "scan_dir", reader(backend))
    root = ScanEngine(workers=1, scan_path=str(every_type)).scan(str(every_type))
    signature = sorted(
        (
            node.path,
            node.is_dir,
            node.size,
            node.allocated_size,
            node.file_count,
            node.dir_count,
            node.is_symlink,
        )
        for node in root.walk()
    )

    monkeypatch.setattr(scheduler_module, "scan_dir", _scanfast_py.scan_dir)
    expected_root = ScanEngine(
        workers=1, scan_path=str(every_type)
    ).scan(str(every_type))
    expected = sorted(
        (
            node.path,
            node.is_dir,
            node.size,
            node.allocated_size,
            node.file_count,
            node.dir_count,
            node.is_symlink,
        )
        for node in expected_root.walk()
    )

    assert signature == expected
    # A socket and a fifo are counted in the chunk and kept out of the tree,
    # the way `os.scandir`'s three type questions used to leave them out.
    assert {node.name for node in root.children} == {
        "regular.bin", "subdir", "link-ok", "link-broken",
    } | {name for name in os.listdir(every_type) if name.startswith("not-utf8-")}


@pytest.mark.parametrize("backend", BACKENDS)
def test_chunk_checkpoints_still_land_every_entry_chunk_size(
    backend, tmp_path, monkeypatch
):
    """The read is whole-directory; the flush is not.

    A directory with more entries than a chunk holds still publishes on the
    same boundaries, so a live scan of a very large directory updates as it
    goes instead of once at the end.
    """
    for index in range(1000):
        (tmp_path / f"f-{index:04d}.bin").write_bytes(b"x")
    monkeypatch.setattr(scheduler_module, "scan_dir", reader(backend))
    chunks = []

    result = scan_directory_once(
        DirectoryJob(str(tmp_path), 0, None),
        policy=ScanPolicy(),
        cancel_event=threading.Event(),
        root_device=None,
        excluded_mounts={},
        checkpoint_callback=lambda chunk: chunks.append(chunk.entry_count),
        entry_chunk_size=256,
    )

    assert result.node.error is None
    assert chunks == [256, 256, 256, 232]


# --- selection --------------------------------------------------------------


def test_the_environment_can_force_the_fallback():
    assert accel.accel_disabled({"DISKTIDE_ACCEL": "0"}) is True
    assert accel.accel_disabled({"DISKTIDE_ACCEL": "off"}) is True
    assert accel.accel_disabled({"DISKTIDE_ACCEL": " No "}) is True
    assert accel.accel_disabled({"DISKTIDE_ACCEL": "1"}) is False
    assert accel.accel_disabled({}) is False


def test_the_chosen_backend_is_one_of_the_two_and_says_why():
    assert accel.ACCEL_BACKEND in BACKENDS
    assert accel.ACCEL_REASON
    if accel.ACCEL_BACKEND == "native":
        assert accel.NATIVE_AVAILABLE
        assert accel.scan_dir is accel.native_scan_dir
    else:
        assert accel.scan_dir is _scanfast_py.scan_dir
        assert accel.NATIVE_AVAILABLE or "not built" in accel.ACCEL_REASON
