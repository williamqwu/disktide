"""Extended cleanup tests — real filesystem operations."""

import os
import tempfile

import pytest
from sizetrail.scanner.walker import scan_directory
from sizetrail.cleanup.detector import detect_targets, group_by_category, total_savings
from sizetrail.cleanup.actions import delete_targets, CleanupResult
from sizetrail.models.patterns import CleanupRule, CleanupTarget, RiskLevel


class TestDetectorRealFS:
    def test_detect_pycache_real(self, tmp_path):
        """Detect __pycache__ on a real filesystem."""
        src = tmp_path / "project"
        src.mkdir()
        cache = src / "__pycache__"
        cache.mkdir()
        (cache / "module.cpython-311.pyc").write_bytes(b"\x00" * 100)
        (src / "main.py").write_text("print('hello')")

        root = scan_directory(str(src))
        targets = detect_targets(root)
        paths = {t.path for t in targets}
        assert str(cache) in paths

    def test_detect_ds_store_real(self, tmp_path):
        ds = tmp_path / ".DS_Store"
        ds.write_bytes(b"\x00" * 8)

        root = scan_directory(str(tmp_path))
        targets = detect_targets(root)
        paths = {t.path for t in targets}
        assert str(ds) in paths

    def test_no_detect_in_empty_dir(self, tmp_path):
        root = scan_directory(str(tmp_path))
        targets = detect_targets(root)
        assert len(targets) == 0

    def test_detect_with_custom_rules(self, tmp_path):
        (tmp_path / "custom_junk").mkdir()
        (tmp_path / "custom_junk" / "data").write_bytes(b"\x00" * 50)

        root = scan_directory(str(tmp_path))
        custom_rule = CleanupRule(
            name="custom", description="Custom junk",
            patterns=["custom_junk"], risk=RiskLevel.SAFE,
            category="custom",
        )
        targets = detect_targets(root, rules=[custom_rule])
        assert len(targets) == 1
        assert targets[0].rule.name == "custom"


class TestDeleteRealFS:
    def test_delete_directory(self, tmp_path):
        target_dir = tmp_path / "to_delete"
        target_dir.mkdir()
        (target_dir / "file1.txt").write_text("a")
        (target_dir / "file2.txt").write_text("b")

        targets = [
            CleanupTarget(
                path=str(target_dir), size=2,
                rule=CleanupRule(name="t", description="", patterns=[]),
            )
        ]
        result = delete_targets(targets)
        assert len(result.successful) == 1
        assert not target_dir.exists()

    def test_delete_directory_symlink_only_unlinks_symlink(self, tmp_path):
        target_dir = tmp_path / "real_data"
        target_dir.mkdir()
        (target_dir / "keep.txt").write_text("keep")
        target_link = tmp_path / "linked_cache"
        target_link.symlink_to(target_dir, target_is_directory=True)

        targets = [
            CleanupTarget(
                path=str(target_link), size=4,
                rule=CleanupRule(name="t", description="", patterns=[]),
            )
        ]

        result = delete_targets(targets)

        assert len(result.successful) == 1
        assert not target_link.exists()
        assert target_dir.exists()
        assert (target_dir / "keep.txt").exists()

    def test_delete_with_progress(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("test")

        progress_calls = []
        targets = [
            CleanupTarget(
                path=str(f), size=4,
                rule=CleanupRule(name="t", description="", patterns=[]),
            )
        ]
        result = delete_targets(
            targets,
            progress_callback=lambda c, t, p: progress_calls.append((c, t)),
        )
        assert len(progress_calls) >= 1
        assert result.total_freed == 4

    def test_delete_mixed_success_failure(self, tmp_path):
        good = tmp_path / "good.txt"
        good.write_text("ok")

        rule = CleanupRule(name="t", description="", patterns=[])
        targets = [
            CleanupTarget(path=str(good), size=2, rule=rule),
            CleanupTarget(path="/nonexistent/bad", size=100, rule=rule),
        ]
        result = delete_targets(targets)
        assert len(result.successful) == 1
        assert len(result.failed) == 1
        assert result.total_freed == 2
        assert result.total_errors == 1


class TestCleanupResult:
    def test_empty_result(self):
        r = CleanupResult()
        assert r.total_freed == 0
        assert r.total_errors == 0
        assert r.successful == []
        assert r.failed == []
