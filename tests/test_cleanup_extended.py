"""Extended cleanup tests — real filesystem operations."""

import os
import tempfile

import pytest
from disktide.scanner import scheduler as scheduler_module
from disktide.scanner.walker import scan_directory
from disktide.cleanup.detector import detect_targets, group_by_category, total_savings
from disktide.cleanup.actions import delete_targets, CleanupResult
from disktide.models.patterns import CleanupRule, CleanupTarget, RiskLevel


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


class TestPathsLongerThanPathMax:
    """Where the mutation path stops, now that the walk does not.

    The scan reaches past PATH_MAX -- every syscall below the scan root is
    made relative to a directory descriptor -- and `classify_symlink` was
    taught the same trick, so a user can now select a row whose path the
    kernel will not accept as an argument. The cleanup path was *not*: it
    names whole paths in `identity_from_path`, `_open_directory`,
    `XDGTrashAdapter.move`, `QuarantineExecutor.move` and `available_bytes`,
    and it is only fd-relative below the parent it has already opened.

    That is a limitation, not a hazard, and this is the test that says so:
    the mutation refuses at token creation with a clear error, before
    anything has been renamed or unlinked. If someone makes the cleanup path
    deep-safe, this test is the one to rewrite.
    """

    @staticmethod
    def _deep_file(root, levels: int = 1200, name: str = "abcdefgh") -> str:
        handle = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        parts = [str(root)]
        try:
            for _ in range(levels):
                os.mkdir(name, dir_fd=handle)
                parts.append(name)
                nested = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=handle
                )
                os.close(handle)
                handle = nested
            with open(
                os.open("junk.tmp", os.O_WRONLY | os.O_CREAT, 0o644,
                        dir_fd=handle),
                "wb",
            ) as leaf:
                leaf.write(b"x" * 32)
        finally:
            os.close(handle)
        return "/".join(parts) + "/junk.tmp"

    def test_a_mutation_token_refuses_a_path_it_cannot_name(self, tmp_path):
        """`create_mutation_token` is where it stops, and it stops cleanly.

        Both `lstat`s in it name a whole path, so both raise ENAMETOOLONG;
        the function catches `OSError` and re-raises the cleanup error the
        callers handle.
        """
        from disktide.cleanup.actions import (
            CleanupExecutionError,
            create_mutation_token,
        )
        from disktide.domain.cleanup import FileIdentity

        deep = self._deep_file(tmp_path)
        assert len(deep) > 4096, "the fixture has to outgrow PATH_MAX"

        identity = FileIdentity(
            device=0, inode=0, mode=0o100644, size=32, mtime_ns=0,
            is_dir=False, is_symlink=False,
        )
        with pytest.raises(CleanupExecutionError) as caught:
            create_mutation_token(deep, identity)
        assert "mutation token creation failed" in str(caught.value)
        assert "too long" in str(caught.value).lower()

    def test_nothing_is_deleted_when_the_path_cannot_be_named(self, tmp_path):
        """`permanent_delete` refuses before it has touched anything.

        Its own first call is an unguarded `identity_from_path`, so what
        reaches the caller here is a bare `OSError` and not the
        `CleanupExecutionError` the rest of the mutation path raises. That is
        the one rough edge of this boundary; nothing is deleted either way,
        which is what this test is really for.
        """
        from disktide.cleanup.actions import permanent_delete

        deep = self._deep_file(tmp_path)
        with pytest.raises(OSError) as caught:
            permanent_delete(deep)
        assert "too long" in str(caught.value).lower()

        # Still there: the refusal happens before anything is touched.
        parent_fd = scheduler_module.open_scan_directory(
            os.path.dirname(deep), follow_symlink=False
        )
        try:
            assert "junk.tmp" in os.listdir(parent_fd)
        finally:
            os.close(parent_fd)
