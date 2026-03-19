"""Tests for built-in cleanup rules."""

import pytest
from fs_monitor.cleanup.rules import get_rules, get_rule_by_name, BUILTIN_RULES
from fs_monitor.models.patterns import RiskLevel


class TestBuiltinRules:
    def test_get_rules_returns_enabled(self):
        rules = get_rules()
        assert len(rules) > 0
        assert all(r.enabled for r in rules)

    def test_get_rules_include_disabled(self):
        rules_all = get_rules(include_disabled=True)
        rules_enabled = get_rules(include_disabled=False)
        assert len(rules_all) >= len(rules_enabled)

    def test_get_rule_by_name(self):
        rule = get_rule_by_name("node_modules")
        assert rule is not None
        assert rule.name == "node_modules"
        assert "node_modules" in rule.patterns

    def test_get_rule_by_name_not_found(self):
        assert get_rule_by_name("nonexistent_rule") is None

    def test_node_modules_rule(self):
        rule = get_rule_by_name("node_modules")
        assert rule.risk == RiskLevel.SAFE
        assert "package.json" in rule.parent_indicators
        assert rule.category == "dependencies"

    def test_python_cache_rule(self):
        rule = get_rule_by_name("python_cache")
        assert rule.risk == RiskLevel.SAFE
        assert "__pycache__" in rule.patterns
        assert rule.category == "cache"

    def test_build_outputs_rule(self):
        rule = get_rule_by_name("build_outputs")
        assert rule.risk == RiskLevel.MODERATE
        assert len(rule.parent_indicators) > 0

    def test_system_junk_rule(self):
        rule = get_rule_by_name("system_junk")
        assert rule.risk == RiskLevel.SAFE
        assert ".DS_Store" in rule.patterns

    def test_all_rules_have_required_fields(self):
        for rule in BUILTIN_RULES:
            assert rule.name, f"Rule missing name"
            assert rule.description, f"Rule {rule.name} missing description"
            assert rule.patterns, f"Rule {rule.name} missing patterns"
            assert rule.category, f"Rule {rule.name} missing category"
            assert isinstance(rule.risk, RiskLevel)
