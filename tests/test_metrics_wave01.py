"""Wave 01 correctness fixtures for storage measurement semantics."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from sizetrail.__main__ import cli
from sizetrail.domain.metrics import (
    MetricId,
    StorageMeasurements,
    allocated_bytes_from_stat,
)
from sizetrail.metrics import metric_text, metric_value
from sizetrail.models.tree import FSNode
from sizetrail.scanner.accounting import finalize_unique_allocated
from sizetrail.scanner.engine import ScanEngine
from sizetrail.scanner.policy import MountEntry, discover_pseudo_mounts
from sizetrail.scanner.walker import scan_directory


def test_storage_measurements_preserve_unavailable_values():
    measurements = StorageMeasurements(
        logical_bytes=10,
        allocated_bytes=None,
        unique_allocated_bytes=None,
        file_count=1,
    )
    assert measurements.value(MetricId.LOGICAL) == 10
    assert measurements.value(MetricId.ALLOCATED) is None
    assert measurements.value(MetricId.UNIQUE) is None
    assert measurements.value(MetricId.FILES) == 1


def test_allocated_bytes_missing_is_not_reported_as_zero():
    stat_result = SimpleNamespace(st_size=123)
    assert allocated_bytes_from_stat(stat_result) is None
    node = FSNode(name="x", path="/x", size=123, file_count=1)
    assert metric_value(node, "allocated") is None
    assert metric_text(node, "allocated") == "Unavailable"


def test_sparse_file_separates_logical_and_allocated(tmp_path):
    sparse = tmp_path / "sparse.img"
    with sparse.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024)

    root = ScanEngine(workers=1).scan(str(tmp_path))
    node = root.find(str(sparse))
    assert node is not None
    assert node.size == 64 * 1024 * 1024
    if node.allocated_size is None:
        pytest.skip("platform does not expose st_blocks")
    assert node.allocated_size < node.size
    assert root.allocated_size == node.allocated_size


def test_hardlinks_are_deduplicated_deterministically(tmp_path):
    original = tmp_path / "z-original.bin"
    original.write_bytes(b"x" * 8192)
    first = tmp_path / "a-link.bin"
    second = tmp_path / "b-link.bin"
    os.link(original, first)
    os.link(original, second)

    results = [ScanEngine(workers=workers).scan(str(tmp_path)) for workers in (1, 4)]
    for root in results:
        leaves = sorted((node for node in root.walk() if not node.is_dir), key=lambda n: n.path)
        if leaves[0].allocated_size is None:
            pytest.skip("platform does not expose st_blocks")
        per_entry = leaves[0].allocated_size
        assert root.allocated_size == per_entry * 3
        assert root.unique_allocated_size == per_entry
        assert [node.hardlink_owner_path for node in leaves] == [str(first)] * 3
        assert sum(node.is_hardlink_duplicate for node in leaves) == 2

    assert results[0].unique_allocated_size == results[1].unique_allocated_size


def test_same_inode_number_on_different_devices_is_not_deduplicated():
    root = FSNode(name="root", path="/root", is_dir=True)
    root.children = [
        FSNode(
            name="a", path="/root/a", allocated_size=4096,
            own_allocated_size=4096, device_id=1, inode=7,
            link_count=2, file_count=1,
        ),
        FSNode(
            name="b", path="/root/b", allocated_size=4096,
            own_allocated_size=4096, device_id=2, inode=7,
            link_count=2, file_count=1,
        ),
    ]
    finalize_unique_allocated(root)
    assert root.unique_allocated_size == 8192
    assert all(not child.is_hardlink_duplicate for child in root.children)


def test_one_filesystem_marks_boundary_without_descending(tmp_path):
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "hidden.bin").write_bytes(b"hidden")
    actual_device = os.stat(mounted).st_dev

    node = scan_directory(
        str(mounted),
        root_device=actual_device + 1,
        one_file_system=True,
    )
    assert node.excluded is True
    assert node.filesystem_boundary is True
    assert node.exclusion_reason == "filesystem boundary"
    assert node.children == []
    assert node.size == 0
    assert node.allocated_size == 0


def test_pseudo_mount_is_marked_excluded_without_descending(tmp_path):
    pseudo = tmp_path / "proc"
    pseudo.mkdir()
    (pseudo / "not-storage").write_text("x")

    node = scan_directory(
        str(pseudo),
        excluded_mounts={os.path.realpath(pseudo): "proc"},
    )
    assert node.excluded is True
    assert node.filesystem_type == "proc"
    assert node.exclusion_reason == "pseudo filesystem (proc)"
    assert node.children == []


def test_explicit_pseudo_root_is_allowed_but_nested_mount_is_excluded(tmp_path):
    nested = tmp_path / "sys"
    nested.mkdir()
    entries = [
        MountEntry(str(tmp_path), "proc"),
        MountEntry(str(nested), "sysfs"),
        MountEntry("/somewhere-else", "proc"),
    ]
    assert discover_pseudo_mounts(str(tmp_path), entries) == {
        os.path.realpath(nested): "sysfs"
    }


def test_max_depth_is_visible_policy_omission(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    (child / "file").write_text("x")

    root = ScanEngine(workers=1, max_depth=1).scan(str(tmp_path))
    child_node = root.find(str(child))
    assert child_node is not None
    assert child_node.depth_limited is True
    assert root.depth_limited_subtree_count == 1
    assert root.has_policy_omissions is True


def test_cli_reports_metric_policy_and_all_totals(tmp_path):
    (tmp_path / "data.bin").write_bytes(b"x" * 4096)
    result = CliRunner().invoke(
        cli,
        [
            "scan", str(tmp_path), "--metric", "allocated",
            "--one-file-system", "--exclude-pseudo", "--workers", "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Metric: Allocated" in result.output
    assert "Logical:" in result.output
    assert "Allocated:" in result.output
    assert "Unique on disk:" in result.output
    assert "Policy: one filesystem; exclude pseudo filesystems" in result.output


def test_scanned_root_carries_explicit_policy(tmp_path):
    root = ScanEngine(
        workers=1,
        max_depth=2,
        one_file_system=True,
        exclude_pseudo_filesystems=False,
    ).scan(str(tmp_path))
    assert root.scan_policy is not None
    assert root.scan_policy.one_file_system is True
    assert root.scan_policy.exclude_pseudo_filesystems is False
    assert root.scan_policy.max_depth == 2
