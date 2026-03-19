"""Built-in and user-configurable cleanup rules."""

from __future__ import annotations

from fs_monitor.models.patterns import CleanupRule, RiskLevel

BUILTIN_RULES: list[CleanupRule] = [
    CleanupRule(
        name="node_modules",
        description="Node.js dependencies (can be reinstalled with npm/yarn)",
        patterns=["node_modules"],
        risk=RiskLevel.SAFE,
        parent_indicators=["package.json"],
        category="dependencies",
    ),
    CleanupRule(
        name="python_cache",
        description="Python bytecode cache and tool caches",
        patterns=["__pycache__", ".pytest_cache", ".mypy_cache"],
        risk=RiskLevel.SAFE,
        category="cache",
    ),
    CleanupRule(
        name="python_bytecode",
        description="Compiled Python bytecode files",
        patterns=["*.pyc", "*.pyo"],
        risk=RiskLevel.SAFE,
        category="cache",
    ),
    CleanupRule(
        name="build_outputs",
        description="Build output directories",
        patterns=["build", "dist", ".next"],
        risk=RiskLevel.MODERATE,
        parent_indicators=["setup.py", "pyproject.toml", "package.json", "next.config.js"],
        category="build",
    ),
    CleanupRule(
        name="rust_target",
        description="Rust/Cargo build artifacts",
        patterns=["target"],
        risk=RiskLevel.MODERATE,
        parent_indicators=["Cargo.toml"],
        category="build",
    ),
    CleanupRule(
        name="old_logs",
        description="Log files older than 30 days",
        patterns=["*.log"],
        risk=RiskLevel.SAFE,
        min_age_days=30,
        category="logs",
    ),
    CleanupRule(
        name="system_junk",
        description="OS-generated metadata files",
        patterns=[".DS_Store", "Thumbs.db", "desktop.ini"],
        risk=RiskLevel.SAFE,
        category="junk",
    ),
    CleanupRule(
        name="ide_caches",
        description="IDE configuration and cache directories",
        patterns=[".idea", ".vscode"],
        risk=RiskLevel.MODERATE,
        category="ide",
    ),
]


def get_rules(include_disabled: bool = False) -> list[CleanupRule]:
    """Get all available cleanup rules."""
    if include_disabled:
        return list(BUILTIN_RULES)
    return [r for r in BUILTIN_RULES if r.enabled]


def get_rule_by_name(name: str) -> CleanupRule | None:
    """Find a rule by name."""
    for rule in BUILTIN_RULES:
        if rule.name == name:
            return rule
    return None
