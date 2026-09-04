"""Smoke tests for tool/bench_scan.py, tool/diag_scan.py and tool/dump_tree.py.

These debug scripts are how regressions like the v0.1.5 symlink-scan
slowdown got diagnosed. They depend on a small forward-compat contract
with the scanner (see each script's module docstring); if a future
refactor breaks that contract, we want it to fail loudly in CI, not the
next time someone tries to reach for the bench on a slow cluster.

The tests run each script as a subprocess against a real tmp_path with
a handful of files and assert the expected output shape: the bench
prints `rate=N files/sec`, the diag prints a `=== SCAN COMPLETE in Xs`
banner and either a hotspot table or the explicit "disabled" notice.
We do not assert specific counts or rates; those are environmental.

`dump_tree.py` is the third of the three and the one a scanner change
is diffed with: it has to keep printing every node exactly once, in a
stable order, or a diff of two builds stops meaning anything.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_DIR = REPO_ROOT / "tool"


def _make_tree(root: Path) -> None:
    """Create a tiny but non-trivial tree: a few files at the root and
    one nested subdir with one file, plus one symlink so the symlink
    branch in the scripts gets exercised."""
    (root / "a.txt").write_text("hello")
    (root / "b.txt").write_text("world!")
    sub = root / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("nested")
    os.symlink(sub, root / "link_to_sub")


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    """Run a tool/ script with the repo on sys.path, capture stdout+stderr."""
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        f"{REPO_ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    )
    return subprocess.run(
        [sys.executable, str(TOOL_DIR / script), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


# --- bench_scan ------------------------------------------------------


def test_bench_scan_prints_expected_shape(tmp_path):
    _make_tree(tmp_path)
    cp = _run("bench_scan.py", str(tmp_path), "--workers", "1")
    assert cp.returncode == 0, cp.stderr
    out = cp.stdout
    # The contract is the last line: parseable summary.
    assert "bench: done in" in out
    assert "rate=" in out and "files/sec" in out
    assert "dirs=" in out and "files=" in out and "size=" in out


def test_bench_scan_defaults_to_cwd(tmp_path, monkeypatch):
    """No path argument: scans current directory."""
    _make_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    cp = _run("bench_scan.py", "--workers", "1")
    assert cp.returncode == 0, cp.stderr
    assert "bench: done in" in cp.stdout


def test_bench_scan_live_mode_reports_event_and_visual_metrics(tmp_path):
    _make_tree(tmp_path)
    cp = _run(
        "bench_scan.py",
        str(tmp_path),
        "--workers",
        "1",
        "--mode",
        "live",
    )
    assert cp.returncode == 0, cp.stderr
    assert "bench: events=" in cp.stdout
    assert "batches=" in cp.stdout
    assert "scheduler_queue_hwm=" in cp.stdout
    assert "bench: live_updates=" in cp.stdout
    assert "first_visual=" in cp.stdout
    assert "bench: done in" in cp.stdout


def test_bench_scan_paints_a_chart_on_the_consumer_thread(tmp_path):
    """`--paint` is what makes the live-render cost measurable headlessly.

    A live paint is pure Python and holds the GIL for its whole duration,
    so `--mode live` on its own measures the transport and none of the
    thing that made a live scan on a real terminal ten times slower than
    the same scan with the chart off.
    """
    _make_tree(tmp_path)
    cp = _run(
        "bench_scan.py", str(tmp_path), "--workers", "1", "--mode", "live",
        "--paint", "96x36",
    )
    assert cp.returncode == 0, cp.stderr
    assert "bench: paint=96x36" in cp.stdout
    assert "painted=" in cp.stdout and "skipped=" in cp.stdout
    assert "pacing=duty cycle" in cp.stdout
    assert "bench: done in" in cp.stdout


def test_bench_scan_can_paint_every_frame(tmp_path):
    """The pre-duty-cycle behaviour, kept so the regression stays runnable."""
    _make_tree(tmp_path)
    cp = _run(
        "bench_scan.py", str(tmp_path), "--workers", "1", "--mode", "live",
        "--paint", "96x36", "--paint-every-frame",
    )
    assert cp.returncode == 0, cp.stderr
    assert "pacing=every frame" in cp.stdout
    assert "skipped=0" in cp.stdout


def test_bench_scan_paint_reports_its_own_share_in_json(tmp_path):
    _make_tree(tmp_path)
    cp = _run(
        "bench_scan.py", str(tmp_path), "--workers", "1", "--mode", "live",
        "--paint", "96x36", "--json",
    )
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    paint = payload["paint"]
    assert paint["size"] == "96x36"
    assert paint["every_frame"] is False
    assert paint["painted"] >= 1
    assert paint["paint_seconds"] > 0.0


def test_bench_scan_rejects_a_paint_size_it_cannot_use(tmp_path):
    _make_tree(tmp_path)
    bad = _run(
        "bench_scan.py", str(tmp_path), "--mode", "live", "--paint", "wide",
    )
    assert bad.returncode == 2
    assert "COLSxROWS" in bad.stderr

    wrong_mode = _run(
        "bench_scan.py", str(tmp_path), "--mode", "raw", "--paint", "96x36",
    )
    assert wrong_mode.returncode == 2
    assert "--mode live" in wrong_mode.stderr


def test_bench_scan_json_is_machine_readable(tmp_path):
    _make_tree(tmp_path)
    cp = _run(
        "bench_scan.py",
        str(tmp_path),
        "--workers",
        "1",
        "--mode",
        "live",
        "--json",
    )
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["benchmark"] == "scan"
    assert payload["worker_selection"]["effective_workers"] == 1
    assert payload["scheduler"]["entry_chunk_size"] > 0


# --- diag_scan -------------------------------------------------------


def test_diag_scan_prints_completion_banner(tmp_path):
    _make_tree(tmp_path)
    cp = _run("diag_scan.py", str(tmp_path), "--workers", "1")
    assert cp.returncode == 0, cp.stderr
    out = cp.stdout
    assert "=== SCAN COMPLETE in" in out
    assert "files:" in out and "dirs:" in out and "size:" in out
    # Either we got a real hotspot table, or the explicit fallback line.
    assert (
        "Top " in out and "directories by wall-clock time" in out
    ) or (
        "hotspots: (no per-directory timing recorded)" in out
    )


def test_diag_scan_with_profile_dumps_cprofile_table(tmp_path):
    """--profile must produce a cProfile section so we can chase NFS
    or threading hot paths in the future."""
    _make_tree(tmp_path)
    cp = _run("diag_scan.py", str(tmp_path), "--workers", "1", "--profile")
    assert cp.returncode == 0, cp.stderr
    out = cp.stdout
    assert "=== SCAN COMPLETE in" in out
    assert "cProfile top callees by cumulative time" in out
    # cProfile's output always names a function call count + cumtime header.
    assert "ncalls" in out and "cumtime" in out


def test_diag_scan_imports_cleanly_even_if_walker_changes(tmp_path, monkeypatch):
    """If `walker.scan_directory` ever moves or its signature changes
    in a way that breaks the monkey-patch, the script must still run
    (heartbeat + summary) and print the disabled notice. We simulate
    a broken patch by stripping the symbol before the script imports."""
    _make_tree(tmp_path)
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        f"{REPO_ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    )
    # -c shim that nukes walker.scan_directory before diag_scan imports
    # walker for monkey-patching, then exec's the script.
    shim = (
        "import sys, runpy; "
        "from disktide.scanner import walker; "
        "del walker.scan_directory; "
        f"sys.argv = ['diag_scan.py', {str(tmp_path)!r}, '--workers', '1']; "
        f"runpy.run_path({str(TOOL_DIR / 'diag_scan.py')!r}, run_name='__main__')"
    )
    cp = subprocess.run(
        [sys.executable, "-c", shim],
        capture_output=True, text=True, env=env, timeout=60,
    )
    # Either the script ran (degraded mode) or, if the import itself
    # blew up, we expect a sensible message on stderr. We accept both.
    if cp.returncode == 0:
        assert "=== SCAN COMPLETE in" in cp.stdout
        assert "hotspot table disabled" in cp.stderr or \
               "hotspots: (no per-directory timing recorded)" in cp.stdout
    else:
        # If the contract really did break, surface what we got so the
        # failure points at the right thing.
        assert "scan_directory" in (cp.stdout + cp.stderr), \
            f"unexpected failure:\nSTDOUT:\n{cp.stdout}\nSTDERR:\n{cp.stderr}"


# --- dump_tree -------------------------------------------------------


def test_dump_tree_emits_one_sorted_line_per_node(tmp_path):
    """The byte-identity harness: every node, one line, sorted by path."""
    _make_tree(tmp_path)
    cp = _run("dump_tree.py", str(tmp_path))
    assert cp.returncode == 0, cp.stderr
    lines = cp.stdout.splitlines()
    # Two header lines: which directory reader produced the dump, then the
    # columns. The backend line is the only one allowed to differ between
    # two dumps of the same tree.
    backend, header, rows = lines[0], lines[1], lines[2:]
    assert backend in ("#backend=native", "#backend=python")
    assert header.startswith("#path\tis_dir\t")
    assert header.split("\t")[1:] == list(
        (
            "is_dir", "size", "allocated_size", "unique_allocated_size",
            "file_count", "dir_count", "error", "vanished", "excluded",
            "depth_limited",
        )
    )
    # root, two files, one subdir, one nested file, one symlink
    assert len(rows) == 6
    assert rows == sorted(rows)
    assert all(len(row.split("\t")) == len(header.split("\t")) for row in rows)
    by_path = {row.split("\t")[0]: row.split("\t") for row in rows}
    assert by_path[str(tmp_path)][1] == "1"
    assert by_path[str(tmp_path / "a.txt")][1] == "0"
    assert by_path[str(tmp_path / "a.txt")][2] == "5"


def test_dump_tree_backend_flag_selects_the_reader(tmp_path):
    """`--backend` picks the directory reader and the header says which.

    The bodies have to match: the two readers exist to produce the same
    tree, and this is the check that runs on every commit rather than only
    when somebody remembers to diff two fixtures.
    """
    _make_tree(tmp_path)
    dumps = {}
    for backend in ("native", "python"):
        cp = _run("dump_tree.py", str(tmp_path), "--backend", backend)
        assert cp.returncode == 0, cp.stderr
        dumps[backend] = cp.stdout.splitlines()
    assert dumps["python"][0] == "#backend=python"
    # The extension is optional; where it is not built, `--backend native`
    # honestly reports the fallback rather than pretending.
    assert dumps["native"][0] in ("#backend=native", "#backend=python")
    assert dumps["native"][1:] == dumps["python"][1:]


def test_dump_tree_writes_a_file_when_asked(tmp_path):
    _make_tree(tmp_path)
    out = tmp_path.parent / "dump.txt"
    cp = _run("dump_tree.py", str(tmp_path), "-o", str(out))
    assert cp.returncode == 0, cp.stderr
    assert "dump_tree: 6 nodes ->" in cp.stderr
    written = out.read_text().splitlines()
    assert written[0].startswith("#backend=")
    assert written[1].startswith("#path\t")
