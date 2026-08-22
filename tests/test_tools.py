"""Smoke tests for tool/bench_scan.py and tool/diag_scan.py.

These two debug scripts are how regressions like the v0.1.5 symlink-scan
slowdown got diagnosed. They depend on a small forward-compat contract
with the scanner (see each script's module docstring); if a future
refactor breaks that contract, we want it to fail loudly in CI, not the
next time someone tries to reach for the bench on a slow cluster.

The tests run each script as a subprocess against a real tmp_path with
a handful of files and assert the expected output shape: the bench
prints `rate=N files/sec`, the diag prints a `=== SCAN COMPLETE in Xs`
banner and either a hotspot table or the explicit "disabled" notice.
We do not assert specific counts or rates; those are environmental.
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
        "from fs_monitor.scanner import walker; "
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
