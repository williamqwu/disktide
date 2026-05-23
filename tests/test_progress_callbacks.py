"""Tests for live per-directory progress callbacks during scan.

Before v0.1.6 the walker reported nothing as it descended; the engine
only updated progress when a whole top-level subdir completed, so a
single big subtree froze the overlay for minutes ("stuck at 97%"). The
walker now ticks per directory finished, and the engine folds those
ticks into shared live counters so Dirs/Files/Size climb continuously.
"""

from fs_monitor.scanner.engine import ScanEngine
from fs_monitor.scanner.walker import scan_directory


def test_walker_ticks_once_per_directory(tmp_path):
    """on_dir_done fires for every directory the walker finishes."""
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "a" / "b" / "c" / "f.txt").write_text("x")

    calls = []
    scan_directory(
        str(tmp_path),
        on_dir_done=lambda d, f, s, p: calls.append((d, f, s, p)),
    )

    # 4 directories: tmp_path, a, b, c
    assert len(calls) == 4
    assert all(c[0] == 1 for c in calls)
    # Files: just the one f.txt, ticked by its containing dir (c).
    assert sum(c[1] for c in calls) == 1
    # Bytes: "x" is one byte.
    assert sum(c[2] for c in calls) == 1


def test_walker_local_counts_only(tmp_path):
    """Each tick reports the directory's LOCAL files/bytes only. Subtree
    files come from the children's own ticks, so summing tick deltas
    equals the totals without double-counting."""
    (tmp_path / "a.txt").write_text("aa")    # 2 bytes
    (tmp_path / "b.txt").write_text("bbb")   # 3 bytes
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("c")          # 1
    (sub / "d.txt").write_text("dd")         # 2
    (sub / "e.txt").write_text("eee")        # 3

    calls = []
    scan_directory(
        str(tmp_path),
        on_dir_done=lambda d, f, s, p: calls.append((d, f, s, p)),
    )

    # 2 dir ticks: tmp_path (root) and sub.
    assert len(calls) == 2
    assert sum(c[0] for c in calls) == 2     # dirs
    assert sum(c[1] for c in calls) == 5     # files
    assert sum(c[2] for c in calls) == 11    # bytes


def test_walker_without_callback_still_scans(tmp_path):
    """on_dir_done is optional; passing None must not break scanning."""
    (tmp_path / "f.txt").write_text("hello")

    node = scan_directory(str(tmp_path))

    assert node.file_count == 1
    assert node.size == 5


def test_engine_progress_callback_fires_and_final_totals_match(tmp_path):
    """The engine threads its _tick into the walker; at least one progress
    snapshot fires, and the final tree totals are correct."""
    for i in range(20):
        d = tmp_path / f"d{i:02}"
        d.mkdir()
        (d / "f.txt").write_text("xxxx")   # 4 bytes each

    snapshots = []
    engine = ScanEngine(
        workers=2,
        progress_callback=lambda p: snapshots.append(
            (p.dirs_scanned, p.files_scanned, p.total_size)
        ),
    )
    root = engine.scan(str(tmp_path))

    assert len(snapshots) >= 1
    assert root.dir_count == 20
    assert root.file_count == 20
    assert root.size == 80
