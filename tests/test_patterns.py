"""Tests for cleanup patterns and rules."""

import pytest
from sizetrail.models.patterns import CleanupRule, CleanupTarget, RiskLevel


class TestCleanupRule:
    def test_matches_exact_name(self):
        rule = CleanupRule(name="test", description="", patterns=["node_modules"])
        assert rule.matches_name("node_modules")
        assert not rule.matches_name("node_modules_backup")
        assert not rule.matches_name("my_modules")

    def test_matches_extension(self):
        rule = CleanupRule(name="test", description="", patterns=["*.pyc"])
        assert rule.matches_name("foo.pyc")
        assert rule.matches_name("bar.pyc")
        assert not rule.matches_name("foo.py")
        assert not rule.matches_name("pyc")

    def test_matches_multiple_patterns(self):
        rule = CleanupRule(
            name="test", description="",
            patterns=["__pycache__", ".pytest_cache", "*.pyc"],
        )
        assert rule.matches_name("__pycache__")
        assert rule.matches_name(".pytest_cache")
        assert rule.matches_name("test.pyc")
        assert not rule.matches_name("src")

    def test_matches_trailing_slash_stripped(self):
        rule = CleanupRule(name="test", description="", patterns=["node_modules/"])
        assert rule.matches_name("node_modules")

    def test_has_parent_indicator_no_indicators(self):
        rule = CleanupRule(name="test", description="", patterns=["test"])
        assert rule.has_parent_indicator("/any/path")

    def test_has_parent_indicator_missing(self, tmp_path):
        rule = CleanupRule(
            name="test", description="", patterns=["test"],
            parent_indicators=["package.json"],
        )
        assert not rule.has_parent_indicator(str(tmp_path))

    def test_has_parent_indicator_present(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        rule = CleanupRule(
            name="test", description="", patterns=["test"],
            parent_indicators=["package.json"],
        )
        assert rule.has_parent_indicator(str(tmp_path))


class TestCleanupTarget:
    def test_risk_delegates_to_rule(self):
        rule = CleanupRule(name="r", description="", patterns=[], risk=RiskLevel.MODERATE)
        target = CleanupTarget(path="/a", size=100, rule=rule)
        assert target.risk == RiskLevel.MODERATE

    def test_category_delegates_to_rule(self):
        rule = CleanupRule(name="r", description="", patterns=[], category="build")
        target = CleanupTarget(path="/a", size=100, rule=rule)
        assert target.category == "build"


class TestRiskLevel:
    def test_values(self):
        assert RiskLevel.SAFE.value == "safe"
        assert RiskLevel.MODERATE.value == "moderate"
        assert RiskLevel.DANGEROUS.value == "dangerous"
