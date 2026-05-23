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


# --- live tree snapshots (the explorer's "render-as-we-scan" path) ----

def test_engine_tree_callback_fires_initial_and_final(tmp_path):
    """tree_callback fires at least twice: once with just the top-level
    files/symlinks before workers run, and once at the very end with
    the fully-aggregated root."""
    # Two top-level files + two subdirs with their own file each.
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("yy")
    (tmp_path / "sub1").mkdir()
    (tmp_path / "sub1" / "c.txt").write_text("zzz")
    (tmp_path / "sub2").mkdir()
    (tmp_path / "sub2" / "d.txt").write_text("wwww")

    snaps = []
    engine = ScanEngine(
        workers=1,
        tree_callback=lambda n: snaps.append(n),
        tree_callback_interval=0.0,  # disable throttling for the test
    )
    root = engine.scan(str(tmp_path))

    assert len(snaps) >= 2

    # First snapshot: just the top-level files (subdir results have not
    # come back yet), so own_size matches and the only children present
    # are the two text files at the root.
    first = snaps[0]
    assert all(not c.is_dir for c in first.children)
    assert first.own_size == 3
    assert first.size == 3

    # Last snapshot: matches the final root exactly.
    last = snaps[-1]
    assert last.size == root.size
    assert last.file_count == root.file_count
    assert last.dir_count == root.dir_count
    assert root.size == 1 + 2 + 3 + 4   # = 10 bytes total


def test_engine_tree_callback_snapshots_are_independent(tmp_path):
    """Each snapshot has its own children list so mutating one cannot
    leak into another (the UI may hold the previous snapshot while a
    new one is forming on the engine thread)."""
    for i in range(5):
        d = tmp_path / f"d{i}"
        d.mkdir()
        (d / "f").write_text("a")

    snaps = []
    engine = ScanEngine(
        workers=2,
        tree_callback=lambda n: snaps.append(n),
        tree_callback_interval=0.0,
    )
    engine.scan(str(tmp_path))

    assert len(snaps) >= 2
    # Distinct list objects, even if some contain the same FSNode children.
    ids = {id(s.children) for s in snaps}
    assert len(ids) == len(snaps)


def test_engine_tree_callback_optional(tmp_path):
    """Scans without a tree_callback behave exactly like v0.1.5."""
    (tmp_path / "f.txt").write_text("hello")
    root = ScanEngine(workers=1).scan(str(tmp_path))  # no tree_callback
    assert root.file_count == 1
    assert root.size == 5


def test_engine_tree_callback_throttled(tmp_path):
    """With a generous throttle interval, the engine still fires the
    forced initial emit and the unconditional final emit, but coalesces
    the per-future emits in between."""
    for i in range(20):
        d = tmp_path / f"d{i:02}"
        d.mkdir()
        (d / "f").write_text("x")

    snaps = []
    engine = ScanEngine(
        workers=2,
        tree_callback=lambda n: snaps.append(n),
        tree_callback_interval=60.0,  # effectively no mid-scan emits
    )
    engine.scan(str(tmp_path))

    # initial (forced) + final (unconditional) at minimum.
    assert len(snaps) >= 2
    # And the final one is fully aggregated.
    assert snaps[-1].file_count == 20
    assert snaps[-1].dir_count == 20
