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


# --- names, links and inodes ------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_name_that_is_not_utf8_survives_as_the_same_bytes(backend, every_type):
    """Both readers decode with the filesystem error handler, not strictly.

    A name the filesystem accepts and UTF-8 does not has to come back as the
    surrogate-escaped `str` `os.listdir` would have produced, because that is
    the string the scheduler joins into a child path and hands to the next
    `open` -- and a reader that produced anything else would name a file
    nothing can open.
    """
    raw = b"not-utf8-\xff\xfe"
    names = [name for name, *_ in read(backend, every_type) if "not-utf8-" in name]

    assert names == [os.fsdecode(raw)]
    assert os.fsencode(names[0]) == raw
    # The round trip is the point: this string still names the file.
    assert os.lstat(os.path.join(os.fsencode(every_type), raw)).st_size == 3


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_symlink_to_a_directory_is_the_link_and_not_the_directory(
    backend, tmp_path
):
    """`AT_SYMLINK_NOFOLLOW`, seen from the one case where following shows.

    A link to a directory is the only entry whose `d_type` and whose followed
    `stat` disagree about being a directory, and following it is how a scanner
    counts a subtree twice or walks a cycle for ever.
    """
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "inside.bin").write_bytes(b"x" * 64)
    os.symlink("real", tmp_path / "link-to-dir")
    rows = {name: row for name, *row in read(backend, tmp_path)}

    dtype, err, mode, size, *_ = rows["link-to-dir"]
    assert dtype == DT_LNK
    assert err == 0
    assert stat.S_ISLNK(mode)
    assert not stat.S_ISDIR(mode)
    assert size == len("real")
    # The directory it points at is still its own entry, and still unstatted.
    assert rows["real"][0] == DT_DIR
    assert rows["real"][2] is None


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_hardlink_pair_reports_one_inode_under_two_names(backend, tmp_path):
    """The identity triple, which is the whole input to unique accounting."""
    original = tmp_path / "original.bin"
    original.write_bytes(b"x" * 4096)
    os.link(original, tmp_path / "second.bin")
    rows = {name: row for name, *row in read(backend, tmp_path)}

    # (d_type, errno, mode, size, blocks, dev, ino, nlink, mtime)
    first, second = rows["original.bin"], rows["second.bin"]
    assert first[5] == second[5]           # st_dev
    assert first[6] == second[6]           # st_ino
    assert first[7] == second[7] == 2      # st_nlink -- what the scan keys on
    assert first[3] == second[3] == 4096
    assert first[4] == second[4]           # st_blocks: the bytes charged once


@pytest.mark.parametrize("backend", BACKENDS)
def test_hardlinked_bytes_are_charged_to_exactly_one_path(
    backend, tmp_path, monkeypatch
):
    """The deciding walk still runs when an inode is shared.

    `ScheduledTree.hardlinked_leaves` skips `finalize_unique_allocated`
    entirely on a tree with no shared inode, which is nearly every tree. This
    is the other half: when the flag is right, the bytes are charged once.
    """
    original = tmp_path / "original.bin"
    original.write_bytes(b"x" * 4096)
    os.link(original, tmp_path / "second.bin")
    monkeypatch.setattr(scheduler_module, "scan_dir", reader(backend))
    root = ScanEngine(workers=1, scan_path=str(tmp_path)).scan(str(tmp_path))

    first = root.find(str(original))
    second = root.find(str(tmp_path / "second.bin"))
    assert first is not None and second is not None
    assert first.allocated_size == second.allocated_size
    # One of the two owns the bytes; the other owns none of them.
    owned = sorted(
        (first.own_unique_allocated_size, second.own_unique_allocated_size)
    )
    assert owned == [0, first.allocated_size]
    # And the owner is the lexically first path, not whichever finished first.
    assert first.hardlink_owner_path == second.hardlink_owner_path == str(original)
    assert root.unique_allocated_size == root.allocated_size - first.allocated_size


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_tree_with_no_shared_inode_mirrors_its_allocated_bytes(
    backend, tmp_path, monkeypatch
):
    """The skip path: unique is a copy of allocated, everywhere, not zero."""
    (tmp_path / "a.bin").write_bytes(b"x" * 2048)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"y" * 1024)
    monkeypatch.setattr(scheduler_module, "scan_dir", reader(backend))
    root = ScanEngine(workers=1, scan_path=str(tmp_path)).scan(str(tmp_path))

    for node in root.walk():
        assert node.unique_allocated_size == node.allocated_size, node.path
        assert node.own_unique_allocated_size == node.own_allocated_size, node.path
        assert node.hardlink_owner_path is None


# --- descriptors that outlive their directory -------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_directory_removed_while_its_descriptor_is_open_reads_as_empty(
    backend, tmp_path
):
    """Opened, then unlinked: the read has to end, not raise and not hang.

    A scan opens a directory and reads it a moment later, and on a live tree
    the directory can be gone in between. The descriptor still refers to the
    unlinked inode, which has no entries left in it, so both readers have to
    agree that the answer is "nothing here" rather than an error.
    """
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    (doomed / "leaf.bin").write_bytes(b"x" * 8)
    handle = os.open(str(doomed), os.O_RDONLY | os.O_DIRECTORY)
    try:
        shutil.rmtree(doomed)
        rows = reader(backend)(handle)
    finally:
        os.close(handle)

    assert rows == []


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_entry_comes_back_with_neither_a_stat_nor_an_errno(
    backend, tmp_path
):
    """Entries vanishing between the readdir and the stat, for real.

    A deleting thread races the read of a directory big enough that it cannot
    finish first. What is asserted is the tuple contract the scheduler's entry
    loop rests on: a non-directory entry either carries stat fields or carries
    an errno saying why it does not. An entry with `errno == 0` and a `None`
    mode reaches `stat.S_ISREG(None)` and takes the worker down with a
    `TypeError`, so this is the shape of the crash, not just of the counts.
    """
    total = 8000
    names = [f"f{index:05d}.bin" for index in range(total)]
    for name in names:
        os.close(os.open(str(tmp_path / name), os.O_CREAT | os.O_WRONLY, 0o644))

    start = threading.Event()
    # Backwards through the second half, which is where the read arrives last.
    # Deleting forwards from the front loses the race every time -- the native
    # reader is through 8,000 entries in 13 ms and stays ahead of the unlinks
    # -- and a race that never happens asserts nothing.
    doomed = list(reversed(names[total // 2:]))

    def delete_from_the_far_end() -> None:
        start.wait(10)
        for name in doomed:
            try:
                os.unlink(tmp_path / name)
            except FileNotFoundError:
                pass

    deleter = threading.Thread(target=delete_from_the_far_end, daemon=True)
    deleter.start()
    handle = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        start.set()
        rows = reader(backend)(handle)
    finally:
        os.close(handle)
        deleter.join(20)

    seen = [name for name, *_ in rows]
    assert len(seen) == len(set(seen)), "an entry was reported twice"
    assert set(seen) <= set(names) | {""}
    for name, dtype, err, mode, *_ in rows:
        statted = mode is not None
        assert statted != bool(err), (
            f"{name!r} carries neither a stat nor an errno "
            f"(d_type={dtype}, errno={err})"
        )
        if err:
            assert err in {errno.ENOENT, errno.ESTALE}, (name, err)
    # Nothing was dropped: everything still on disk at the end was listed.
    assert set(os.listdir(tmp_path)) <= set(seen)


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_scheduler_counts_a_racing_deletion_without_dropping_entries(
    backend, tmp_path, monkeypatch
):
    """The same race, one level up: every entry read is a child or a count.

    Not every *file* -- a name unlinked before `readdir` reaches it may
    legitimately never be listed at all, which is what POSIX allows a
    directory read racing a modification to do. What the scheduler owes is
    that nothing it was handed goes missing between the tuple and the tree.
    """
    total = 4000
    names = [f"f{index:05d}.bin" for index in range(total)]
    for name in names:
        os.close(os.open(str(tmp_path / name), os.O_CREAT | os.O_WRONLY, 0o644))
    start = threading.Event()
    doomed = set(names[total // 2:])

    def delete_from_the_far_end() -> None:
        start.wait(10)
        for name in sorted(doomed, reverse=True):
            try:
                os.unlink(tmp_path / name)
            except FileNotFoundError:
                pass

    deleter = threading.Thread(target=delete_from_the_far_end, daemon=True)
    deleter.start()
    real = reader(backend)
    handed_over = []

    def counting_scan_dir(fd, stat_dirs=False):
        rows = real(fd, stat_dirs)
        handed_over.append(len(rows))
        return rows

    monkeypatch.setattr(scheduler_module, "scan_dir", counting_scan_dir)
    try:
        start.set()
        result = scan_directory_once(
            DirectoryJob(str(tmp_path), 0, None),
            policy=ScanPolicy(),
            cancel_event=threading.Event(),
            root_device=None,
            excluded_mounts={},
        )
    finally:
        deleter.join(20)

    assert result.node.error is None
    accounted = (
        len(result.node.children)
        + result.direct_vanished
        + result.direct_inaccessible
    )
    assert handed_over == [accounted]
    # A deletion is a vanished entry, never a denied one.
    assert result.direct_inaccessible == 0
    # Every name that was never doomed was read, and is in the tree.
    kept = {child.name for child in result.node.children}
    assert set(names) - doomed <= kept


# --- descriptors that run out ------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_reader_that_runs_out_of_descriptors_is_a_denied_directory(
    backend, tmp_path, monkeypatch
):
    """EMFILE from the reader's own `dup`, which is what the C one can raise.

    `_scanfast.c` dups the caller's descriptor for `fdopendir`, exactly as
    `os.scandir(fd)` does, and raises `OSError` when that fails. The scheduler
    has to treat it the way it treated the `OSError` the scandir iterator used
    to raise: this directory is an error, the rest of the scan carries on.
    """
    (tmp_path / "reachable").mkdir()
    (tmp_path / "reachable" / "leaf.bin").write_bytes(b"x" * 8)
    starved = tmp_path / "starved"
    starved.mkdir()
    (starved / "hidden.bin").write_bytes(b"y" * 8)
    real = reader(backend)

    def scan_dir(fd, stat_dirs=False):
        if os.path.basename(os.readlink(f"/proc/self/fd/{fd}")) == "starved":
            raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))
        return real(fd, stat_dirs)

    monkeypatch.setattr(scheduler_module, "scan_dir", scan_dir)
    root = ScanEngine(workers=1, scan_path=str(tmp_path)).scan(str(tmp_path))

    node = root.find(str(starved))
    assert node is not None
    assert node.error is not None
    assert "Too many open files" in node.error
    # Not a vanished directory: nothing went missing, we ran out of room.
    assert node.vanished is False
    assert root.inaccessible_count == 1
    # And the sibling is still there, with its file.
    assert root.find(str(tmp_path / "reachable" / "leaf.bin")) is not None


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_negative_descriptor_raises_rather_than_reading_the_cwd(backend):
    """`-1` is `AT_FDCWD` to `os.scandir`, and a bad descriptor to `dup`.

    `scan_directory_once` keeps `-1` as its "the open failed" sentinel and
    returns before the read, so nothing gets here today. It is pinned because
    the two readers are meant to be one function: the extension answered
    EBADF and the fallback answered with a listing of whatever directory the
    process happened to be sitting in.
    """
    with pytest.raises(OSError) as caught:
        reader(backend)(-1)
    assert caught.value.errno == errno.EBADF
