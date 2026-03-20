"""Tests for PathSuggester and path completion utilities."""

import os

import pytest

from fs_monitor.screens.welcome import PathSuggester, _get_completions


class TestPathSuggester:
    @pytest.fixture
    def tree(self, tmp_path):
        """Create a directory tree for testing."""
        (tmp_path / "documents").mkdir()
        (tmp_path / "downloads").mkdir()
        (tmp_path / "desktop").mkdir()
        (tmp_path / "file.txt").touch()
        (tmp_path / ".hidden").mkdir()
        return tmp_path

    @pytest.mark.asyncio
    async def test_suggest_child_dir(self, tree):
        suggester = PathSuggester()
        result = await suggester.get_suggestion(str(tree) + "/doc")
        assert result == str(tree) + "/documents/"

    @pytest.mark.asyncio
    async def test_trailing_slash_for_dirs(self, tree):
        suggester = PathSuggester()
        result = await suggester.get_suggestion(str(tree) + "/des")
        assert result is not None
        assert result.endswith("/")

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self, tree):
        suggester = PathSuggester()
        result = await suggester.get_suggestion(str(tree) + "/zzz")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_returns_none(self):
        suggester = PathSuggester()
        result = await suggester.get_suggestion("")
        assert result is None

    @pytest.mark.asyncio
    async def test_tilde_expansion(self):
        suggester = PathSuggester()
        result = await suggester.get_suggestion("~/")
        # Should return something starting with ~ if home dir has entries
        if result is not None:
            assert result.startswith("~")

    @pytest.mark.asyncio
    async def test_file_no_trailing_slash(self, tree):
        suggester = PathSuggester()
        result = await suggester.get_suggestion(str(tree) + "/fi")
        assert result == str(tree) + "/file.txt"
        assert not result.endswith("/")

    @pytest.mark.asyncio
    async def test_nonexistent_parent_returns_none(self):
        suggester = PathSuggester()
        result = await suggester.get_suggestion("/nonexistent_dir_xyz/abc")
        assert result is None


class TestGetCompletions:
    @pytest.fixture
    def tree(self, tmp_path):
        (tmp_path / "documents").mkdir()
        (tmp_path / "downloads").mkdir()
        (tmp_path / "desktop").mkdir()
        (tmp_path / "file.txt").touch()
        return tmp_path

    def test_multiple_matches(self, tree):
        matches = _get_completions(str(tree) + "/do")
        assert len(matches) == 2
        names = [os.path.basename(m.rstrip("/")) for m in matches]
        assert "documents" in names
        assert "downloads" in names

    def test_single_match(self, tree):
        matches = _get_completions(str(tree) + "/des")
        assert len(matches) == 1
        assert "desktop" in matches[0]

    def test_no_matches(self, tree):
        matches = _get_completions(str(tree) + "/zzz")
        assert matches == []

    def test_empty_input(self):
        assert _get_completions("") == []

    def test_lists_directory_contents(self, tree):
        """Typing a path ending with / lists all non-hidden entries."""
        matches = _get_completions(str(tree) + "/")
        names = [os.path.basename(m.rstrip("/")) for m in matches]
        assert "documents" in names
        assert "downloads" in names
        assert "desktop" in names
        assert "file.txt" in names

    def test_dirs_have_trailing_slash(self, tree):
        matches = _get_completions(str(tree) + "/des")
        assert len(matches) == 1
        assert matches[0].endswith("/")

    def test_files_no_trailing_slash(self, tree):
        matches = _get_completions(str(tree) + "/fi")
        assert len(matches) == 1
        assert not matches[0].endswith("/")
