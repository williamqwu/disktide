"""Entries that change during a scan are "changed", not "denied".

A tree that is being written to while it is walked -- a build directory, a
scratch area, anything with a job running in it -- hands the scanner an
`ENOENT` between the readdir that listed an entry and the stat that measures
it. That was reported as *unreadable*: the run flipped to PARTIAL, every
removed file raised an `AccessError` reading "[Errno 2] No such file or
directory", and the UI drew the partial glyph. Nothing was denied; the tree
moved. `$S/bench/churn.py` on a 4,000-file copy showed 13 phantom
inaccessible entries, one denied directory and three access-error events in a
0.09 s scan.

These tests are deterministic: the errnos come from stubbed `stat` calls or
from real deletions performed at a known point (after the readdir that listed
them), never from a racing thread.
"""

from __future__ import annotations

import errno
import os
import shutil
import threading

import pytest

from disktide.domain.policy import ScanPolicy
from disktide.domain.scan import AccessError, ScanRequest, ScanStatus
from disktide.scanner import scheduler as scheduler_module
from disktide.scanner import walker as walker_module
from disktide.scanner.scheduler import DirectoryJob, scan_directory_once
from disktide.scanner.walker import scan_directory
from disktide.services.scan import ScanService


VANISHED_EXCEPTIONS = {
    "enoent": FileNotFoundError(errno.ENOENT, "No such file or directory"),
    "estale": OSError(errno.ESTALE, "Stale file handle"),
    "enotdir": NotADirectoryError(errno.ENOTDIR, "Not a directory"),
}


class _Entries:
    """`os.scandir`'s iterator contract over a materialised entry list."""

    def __init__(self, entries):
        self._entries = iter(entries)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._entries)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _StatRaises:
    """A DirEntry whose `stat` raises; everything else is the real entry.

    This is the exact shape of the race: readdir already told us the name and
    the type, and only the stat that follows finds the entry gone.
    """

    def __init__(self, entry, exc):
        self._entry = entry
        self._exc = exc

    def __getattr__(self, name):
        return getattr(self._entry, name)

    def stat(self, *, follow_symlinks=True):
        raise self._exc


def _stub_scandir(monkeypatch, module, wrap):
    """Replace `module.os` with one whose scandir applies `wrap` to entries.

    Shadowing the module's `os` binding rather than assigning to `os.scandir`
    keeps the override off every other thread in the process.
    """
    real_scandir = os.scandir

    class _Shim:
        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def scandir(path):
            with real_scandir(path) as entries:
                listed = list(entries)
            return _Entries(wrap(listed))

    monkeypatch.setattr(module, "os", _Shim())


def _doomed_tree(root):
    """Three surviving files, two doomed files, one doomed subdirectory."""
    for index in range(3):
        (root / f"keep-{index}.bin").write_bytes(b"x" * 16)
    for index in range(2):
        (root / f"gone-{index}.bin").write_bytes(b"y" * 16)
    live = root / "live"
    live.mkdir()
    (live / "leaf.bin").write_bytes(b"z" * 8)
    doomed = root / "gone-dir"
    doomed.mkdir()
    (doomed / "leaf.bin").write_bytes(b"z" * 8)
    return root


def _scan(path, consumers=()):
    return ScanService().scan(
        ScanRequest(path=str(path), workers=1, source="changed-during-scan"),
        consumers=consumers,
    )


class TestVanishedEntries:
    @pytest.mark.parametrize("label", sorted(VANISHED_EXCEPTIONS))
    def test_files_that_vanish_are_counted_apart_from_denials(
        self, tmp_path, monkeypatch, label
    ):
        _doomed_tree(tmp_path)
        exc = VANISHED_EXCEPTIONS[label]

        def wrap(entries):
            return [
                _StatRaises(entry, exc)
                if entry.name.startswith("gone-") and not entry.is_dir(
                    follow_symlinks=False
                )
                else entry
                for entry in entries
            ]

        _stub_scandir(monkeypatch, scheduler_module, wrap)
        errors = []
        run = _scan(tmp_path, consumers=(lambda event: (
            errors.append(event) if isinstance(event, AccessError) else None
        ),))
        root = run.root

        assert root.vanished_count == 2
        assert root.vanished_subtree_count == 2
        assert root.inaccessible_count == 0
        assert root.inaccessible_subtree_count == 0
        assert root.denied_dir_subtree_count == 0
        assert root.partial_dir_subtree_count == 0
        assert errors == []
        assert run.status is ScanStatus.COMPLETED
        assert run.progress.errors == 0
        # The surviving entries are all still there and still measured:
        # three files at the root plus one in each of the two subdirectories.
        assert root.file_count == 5

    def test_permission_error_is_still_an_access_failure(
        self, tmp_path, monkeypatch
    ):
        _doomed_tree(tmp_path)
        denied = PermissionError(errno.EACCES, "Permission denied")

        def wrap(entries):
            return [
                _StatRaises(entry, denied)
                if entry.name.startswith("gone-") and not entry.is_dir(
                    follow_symlinks=False
                )
                else entry
                for entry in entries
            ]

        _stub_scandir(monkeypatch, scheduler_module, wrap)
        errors = []
        run = _scan(tmp_path, consumers=(lambda event: (
            errors.append(event) if isinstance(event, AccessError) else None
        ),))
        root = run.root

        assert root.inaccessible_count == 2
        assert root.inaccessible_subtree_count == 2
        assert root.vanished_count == 0
        assert root.vanished_subtree_count == 0
        assert run.status is ScanStatus.PARTIAL
        assert run.progress.errors == 2
        assert [event.count for event in errors] == [2]

    def test_a_symlink_that_vanishes_is_not_an_access_failure(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "target.bin").write_bytes(b"x" * 16)
        (tmp_path / "gone.lnk").symlink_to(tmp_path / "target.bin")
        exc = VANISHED_EXCEPTIONS["enoent"]

        def wrap(entries):
            return [
                _StatRaises(entry, exc) if entry.name.endswith(".lnk") else entry
                for entry in entries
            ]

        _stub_scandir(monkeypatch, scheduler_module, wrap)
        run = _scan(tmp_path)

        assert run.root.vanished_count == 1
        assert run.root.inaccessible_count == 0
        assert run.status is ScanStatus.COMPLETED

    @pytest.mark.parametrize("label", sorted(VANISHED_EXCEPTIONS))
    def test_a_removed_directory_job_returns_a_vanished_node(
        self, tmp_path, label
    ):
        missing = tmp_path / "already-gone"
        job = DirectoryJob(str(missing), 1, str(tmp_path))
        result = scan_directory_once(
            job,
            policy=ScanPolicy(),
            cancel_event=threading.Event(),
            root_device=None,
            excluded_mounts={},
        )

        assert result.node.vanished is True
        assert result.node.error is None
        assert result.node.size == 0
        assert result.node.children == []
        assert result.direct_vanished == 0

    def test_a_missing_scan_root_is_still_an_error(self, tmp_path):
        # A root that does not exist is a bad argument, not a changed tree:
        # nothing listed it, so nothing can have removed it under us.
        missing = tmp_path / "never-existed"
        job = DirectoryJob(str(missing), 0, None)
        result = scan_directory_once(
            job,
            policy=ScanPolicy(),
            cancel_event=threading.Event(),
            root_device=None,
            excluded_mounts={},
        )

        assert result.node.vanished is False
        assert result.node.error is not None

    def test_a_vanished_child_directory_counts_as_one_vanished_entry(
        self, tmp_path, monkeypatch
    ):
        _doomed_tree(tmp_path)
        removed = threading.Event()
        real_scandir = os.scandir

        class _Shim:
            def __getattr__(self, name):
                return getattr(os, name)

            @staticmethod
            def scandir(path):
                with real_scandir(path) as entries:
                    listed = list(entries)
                # Remove the doomed directory once its parent has listed it,
                # so the scheduler hands out a job for a directory that is
                # already gone -- the case the placeholder node describes.
                if not removed.is_set():
                    shutil.rmtree(tmp_path / "gone-dir")
                    removed.set()
                return _Entries(listed)

        monkeypatch.setattr(scheduler_module, "os", _Shim())
        errors = []
        run = _scan(tmp_path, consumers=(lambda event: (
            errors.append(event) if isinstance(event, AccessError) else None
        ),))
        root = run.root
        gone = root.find(str(tmp_path / "gone-dir"))

        assert gone is not None
        assert gone.vanished is True
        assert gone.error is None
        assert gone.children == []
        # A vanished child directory is one vanished entry for its parent,
        # exactly as a denied child directory is one inaccessible entry.
        assert root.vanished_count == 1
        assert root.vanished_subtree_count == 1
        assert run.status is ScanStatus.COMPLETED
        assert errors == []
        assert root.denied_dir_subtree_count == 0
        assert root.partial_dir_subtree_count == 0
        assert root.inaccessible_subtree_count == 0


class TestDeletionBetweenReaddirAndStat:
    """The churn case with the race taken out: real deletions, fixed point."""

    def _deleting_shim(self, monkeypatch, module):
        real_scandir = os.scandir

        class _Shim:
            def __getattr__(self, name):
                return getattr(os, name)

            @staticmethod
            def scandir(path):
                with real_scandir(path) as entries:
                    listed = list(entries)
                # Decide before deleting: a DirEntry's cached type is what
                # readdir returned, but is_dir can still fall back to a stat.
                doomed = [
                    (entry.path, entry.is_dir(follow_symlinks=False))
                    for entry in listed
                    if entry.name.startswith("gone-")
                ]
                for path_, is_dir in doomed:
                    if is_dir:
                        shutil.rmtree(path_, ignore_errors=True)
                    else:
                        os.unlink(path_)
                return _Entries(listed)

        monkeypatch.setattr(module, "os", _Shim())

    def test_scheduler_reports_a_changed_tree_as_complete(
        self, tmp_path, monkeypatch
    ):
        source = tmp_path / "source"
        source.mkdir()
        _doomed_tree(source)
        for index in range(4):
            branch = source / f"branch-{index}"
            branch.mkdir()
            _doomed_tree(branch)
        work = tmp_path / "work"
        shutil.copytree(source, work, symlinks=True)

        self._deleting_shim(monkeypatch, scheduler_module)
        errors = []
        run = _scan(work, consumers=(lambda event: (
            errors.append(event) if isinstance(event, AccessError) else None
        ),))
        root = run.root

        assert run.status is ScanStatus.COMPLETED
        assert errors == []
        # Five directories each lose two files and one subdirectory between
        # their readdir and the stats that follow it.
        assert root.vanished_subtree_count == 15
        assert sum(1 for node in root.walk() if node.vanished) == 5
        assert root.inaccessible_subtree_count == 0
        assert root.denied_dir_subtree_count == 0
        assert root.partial_dir_subtree_count == 0
        assert run.progress.errors == 0
        assert [node.path for node in root.walk() if node.error is not None] == []
        # Every vanished directory keeps its place in its parent's children
        # list -- the scheduler addresses children by position.
        for node in root.walk():
            if node.vanished:
                assert node.is_dir
                assert node.children == []
                assert node.size == 0

    def test_the_legacy_walker_agrees(self, tmp_path, monkeypatch):
        source = tmp_path / "source"
        source.mkdir()
        _doomed_tree(source)
        for index in range(2):
            branch = source / f"branch-{index}"
            branch.mkdir()
            _doomed_tree(branch)
        work = tmp_path / "work"
        shutil.copytree(source, work, symlinks=True)

        self._deleting_shim(monkeypatch, walker_module)
        root = scan_directory(str(work))

        assert root.vanished_subtree_count == 9
        assert sum(1 for node in root.walk() if node.vanished) == 3
        assert root.inaccessible_subtree_count == 0
        assert root.denied_dir_subtree_count == 0
        assert root.partial_dir_subtree_count == 0
        assert [node.path for node in root.walk() if node.error is not None] == []

    def test_the_legacy_walker_still_reports_a_denial(
        self, tmp_path, monkeypatch
    ):
        _doomed_tree(tmp_path)
        denied = PermissionError(errno.EACCES, "Permission denied")

        def wrap(entries):
            return [
                _StatRaises(entry, denied)
                if entry.name.startswith("gone-") and not entry.is_dir(
                    follow_symlinks=False
                )
                else entry
                for entry in entries
            ]

        _stub_scandir(monkeypatch, walker_module, wrap)
        root = scan_directory(str(tmp_path))

        assert root.inaccessible_count == 2
        assert root.vanished_count == 0


class TestChangedDuringScanSurfaces:
    """The three places a reader can find out that the tree moved."""

    @staticmethod
    def _node(name: str, **kw):
        from disktide.models.tree import FSNode

        kw.setdefault("size", 100)
        kw.setdefault("own_size", 100)
        return FSNode(name=name, path=f"/{name}", is_dir=True, depth=0, **kw)

    def _labels(self):
        """A root with one live and one vanished child, and its labeller."""
        from disktide.widgets.size_tree import SizeTree

        root = self._node("root", size=1000, own_size=0)
        live = self._node("live", size=500, own_size=500)
        gone = self._node("removed", size=0, own_size=0, vanished=True)
        root.children = [live, gone]
        tree = SizeTree(root)
        return tree, live, gone

    def _panel(self, node) -> str:
        from io import StringIO

        from rich.console import Console

        from disktide.widgets.info_panel import InfoPanel

        panel = InfoPanel()
        panel._display = type(
            "Stub", (), {"update": lambda self, x: setattr(self, "last", x)}
        )()
        panel.update_node(node)
        buf = StringIO()
        Console(file=buf, width=200, color_system=None).print(panel._display.last)
        return buf.getvalue()

    def test_a_vanished_directory_is_labelled_gone_without_a_share_bar(self):
        tree, _live, gone = self._labels()
        plain = tree._make_label(gone).plain
        assert "(gone)" in plain
        # A 0.0% bar beside a removed directory reads as a measurement.
        assert "%" not in plain

    def test_a_live_sibling_keeps_its_share_bar(self):
        tree, live, _gone = self._labels()
        plain = tree._make_label(live).plain
        assert "(gone)" not in plain
        assert "50.0%" in plain

    def test_the_access_row_says_removed_during_scan(self):
        out = self._panel(self._node("removed", vanished=True))
        assert "Removed during scan" in out

    def test_the_panel_counts_vanished_entries_below(self):
        out = self._panel(self._node("busy", vanished_subtree_count=4))
        assert "Changed during scan" in out
        assert "4 entries vanished" in out

    def test_one_vanished_entry_is_singular(self):
        out = self._panel(self._node("busy", vanished_subtree_count=1))
        assert "1 entry vanished" in out

    def test_a_clean_directory_says_nothing_about_change(self):
        out = self._panel(self._node("clean"))
        assert "Changed during scan" not in out
