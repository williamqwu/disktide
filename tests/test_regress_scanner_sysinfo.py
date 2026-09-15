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

    def test_auto_still_names_the_share_the_other_users_leave(
        self, tmp_path, unconfined_shared_host
    ):
        """16 cores at load 1, four people on the box: 3.8 cores are ours."""
        selection = select_scan_workers(str(tmp_path), None)
        assert "fair share of 15 idle CPUs across 4 users" in selection.reason


class TestAnAutomountedShareIsNotLocalDisk:
    """The reported case: `autofs` shadowing the NFS mount above it.

    `/proc/self/mounts` holds two records with the identical mountpoint for a
    *direct* automount -- the trigger first, then the filesystem the
    automounter mounted over it. Keeping the first match made
    `select_scan_workers` answer 1 worker with "low-latency local metadata"
    for a mount whose server round trip is 0.65 ms.
    """

    @pytest.fixture
    def automounted(self, monkeypatch, tmp_path):
        from disktide.collectors.platform.base import PlatformAdapter
        from disktide.collectors.platform.models import MountRecord, ProbeResult

        mountpoint = str(tmp_path)

        class _Adapter(PlatformAdapter):
            def enumerate_mounts(self):
                return ProbeResult.available(
                    [
                        MountRecord("/dev/sda1", "/", "ext4"),
                        MountRecord("systemd-1", mountpoint, "autofs"),
                        MountRecord("server:/export", mountpoint, "nfs"),
                    ],
                    "fake mount table",
                )

            def mount_latency(self, mount_point):
                return ProbeResult.available(0.00066, "fake server round trip")

        monkeypatch.setattr(
            "disktide.scanner.sysinfo.get_platform_adapter", lambda *a: _Adapter()
        )
        for name in sysinfo._BATCH_JOB_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(sysinfo, "detect_cpu_count", lambda: (128, 128))
        monkeypatch.setattr(sysinfo, "detect_cpu_quota", lambda: None)
        monkeypatch.setattr(
            sysinfo, "detect_load_average", lambda: (1.0, 1.0, 1.0)
        )
        monkeypatch.setattr(sysinfo, "count_other_users", lambda: 5)
        return mountpoint

    def test_the_mount_is_seen_as_the_network_filesystem_it_is(self, automounted):
        selection = select_scan_workers(automounted, None)
        assert selection.filesystem_type == "nfs"
        assert selection.is_network_fs is True

    def test_and_gets_the_workers_its_round_trip_pays_for(self, automounted):
        selection = select_scan_workers(automounted, None)
        assert selection.effective_workers == 16
        assert selection.mount_latency_seconds == 0.00066
        assert "0.5 ms/entry tier" in selection.reason
        assert "fair share of 127 idle CPUs across 6 users" in selection.reason
