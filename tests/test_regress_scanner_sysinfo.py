"""Regressions for `disktide.scanner.sysinfo` worker selection."""

import pytest

from disktide.scanner import sysinfo
from disktide.scanner.sysinfo import select_scan_workers


@pytest.fixture
def unconfined_shared_host(monkeypatch):
    """An unallocated login node with three other users on it."""
    for name in sysinfo._BATCH_JOB_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sysinfo, "detect_cpu_count", lambda: (16, 16))
    monkeypatch.setattr(sysinfo, "detect_cpu_quota", lambda: None)
    monkeypatch.setattr(sysinfo, "detect_load_average", lambda: (1.0, 1.0, 1.0))
    calls: list[int] = []

    def _count_other_users() -> int:
        calls.append(1)
        return 3

    monkeypatch.setattr(sysinfo, "count_other_users", _count_other_users)
    return calls


class TestExplicitWorkersOnASharedHost:
    """`-w` above the shared-host cap must still say the host is shared."""

    def test_an_explicit_count_warns_about_the_other_users(
        self, tmp_path, unconfined_shared_host
    ):
        selection = select_scan_workers(str(tmp_path), 8)
        assert any(
            "8 workers on a shared host with 3 other active user(s)" in warning
            for warning in selection.warnings
        ), selection.warnings

    def test_the_explicit_path_still_skips_the_latency_sample(
        self, tmp_path, unconfined_shared_host
    ):
        selection = select_scan_workers(str(tmp_path), 8)
        assert selection.sample_outcome == "bypassed-explicit-override"

    def test_a_small_explicit_count_does_not_walk_proc(
        self, tmp_path, unconfined_shared_host
    ):
        """Below the cap the answer cannot change, so do not pay for /proc."""
        selection = select_scan_workers(str(tmp_path), 2)
        assert unconfined_shared_host == []
        assert selection.warnings == ()

    def test_auto_still_names_the_other_users(self, tmp_path, unconfined_shared_host):
        selection = select_scan_workers(str(tmp_path), None)
        assert "3 other active user(s)" in selection.reason
