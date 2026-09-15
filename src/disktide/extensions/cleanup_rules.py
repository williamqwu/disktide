"""Versioned declarative cleanup rule-pack loading and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Iterable

from disktide._compat import tomllib
from disktide.models.patterns import (
    CleanupRule,
    CleanupRuleActionPolicy,
    RiskLevel,
)


RULE_PACK_SCHEMA_VERSION = 1
_PACK_FIELDS = {
    "schema_version",
    "name",
    "version",
    "description",
    "default_enabled",
    "rules",
}
_RULE_FIELDS = {
    "name",
    "description",
    "patterns",
    "parent_indicators",
    "path_context",
    "min_age_days",
    "risk",
    "category",
    "rebuild_hint",
    "confidence",
    "default_action",
}


class RulePackValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CleanupRulePack:
    name: str
    version: str
    schema_version: int
    description: str
    source: str
    enabled: bool
    rules: tuple[CleanupRule, ...]
    path: str | None = None


@dataclass(frozen=True, slots=True)
class RulePackIssue:
    source: str
    path: str
    error: str
    pack_name: str | None = None


@dataclass(slots=True)
class CleanupRuleCatalog:
    packs: tuple[CleanupRulePack, ...]
    issues: tuple[RulePackIssue, ...] = ()
    _rules: tuple[CleanupRule, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rules = tuple(rule for pack in self.packs for rule in pack.rules)

    @property
    def rules(self) -> tuple[CleanupRule, ...]:
        return tuple(rule for rule in self._rules if rule.enabled)

    @property
    def all_rules(self) -> tuple[CleanupRule, ...]:
        return self._rules

    def get_rule(self, name: str) -> CleanupRule | None:
        return next((rule for rule in self._rules if rule.name == name), None)

    def get_pack(self, name: str) -> CleanupRulePack | None:
        return next((pack for pack in self.packs if pack.name == name), None)


def validate_rule_pack(path: str | Path) -> CleanupRulePack:
    value = Path(path)
    try:
        with value.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RulePackValidationError(f"cannot read TOML: {exc}") from exc
    return _pack_from_data(data, source="user", path=str(value))


def load_rule_catalog(
    *,
    disabled_packs: Iterable[str] = (),
    enabled_packs: Iterable[str] = (),
    user_directory: str | Path | None = None,
) -> CleanupRuleCatalog:
    """Load every rule pack, honouring both halves of the config's opinion.

    `default_enabled = false` in a pack means "opt in", so a name in
    `enabled_packs` turns it on -- without that, `cleanup rules enable`
    could only ever undo a `disable`, and reported success for an opt-in
    pack that stayed off. `disabled_packs` still wins: an explicit "off"
    is the more recent instruction either way.
    """
    disabled = set(disabled_packs)
    opted_in = set(enabled_packs)
    packs: list[CleanupRulePack] = []
    issues: list[RulePackIssue] = []
    seen_packs: set[str] = set()
    seen_rules: set[str] = set()

    builtins = files("disktide.cleanup.rulepacks")
    candidates: list[tuple[str, object]] = [
        ("builtin", item)
        for item in sorted(builtins.iterdir(), key=lambda item: item.name)
        if item.name.endswith(".toml")
    ]
    if user_directory is not None:
        directory = Path(user_directory)
        if directory.exists():
            candidates.extend(
                ("user", item) for item in sorted(directory.glob("*.toml"))
            )

    for source, candidate in candidates:
        display_path = getattr(candidate, "name", str(candidate))
        data = None
        try:
            with candidate.open("rb") as stream:
                data = tomllib.load(stream)
            pack = _pack_from_data(data, source=source, path=str(candidate))
            if pack.name in seen_packs:
                raise RulePackValidationError(
                    f"duplicate pack name '{pack.name}'"
                )
            duplicate_rules = sorted(
                rule.name for rule in pack.rules if rule.name in seen_rules
            )
            if duplicate_rules:
                raise RulePackValidationError(
                    "duplicate rule name(s): " + ", ".join(duplicate_rules)
                )
            enabled = (
                pack.enabled or pack.name in opted_in
            ) and pack.name not in disabled
            if enabled != pack.enabled:
                pack = CleanupRulePack(
                    name=pack.name,
                    version=pack.version,
                    schema_version=pack.schema_version,
                    description=pack.description,
                    source=pack.source,
                    enabled=enabled,
                    rules=tuple(
                        _copy_rule(rule, enabled=enabled) for rule in pack.rules
                    ),
                    path=pack.path,
                )
            seen_packs.add(pack.name)
            seen_rules.update(rule.name for rule in pack.rules)
            packs.append(pack)
        except (OSError, tomllib.TOMLDecodeError, RulePackValidationError) as exc:
            pack_name = None
            if isinstance(data, dict):
                raw_name = data.get("name")
                pack_name = str(raw_name) if raw_name else None
            issues.append(
                RulePackIssue(
                    source=source,
                    path=display_path,
                    error=str(exc),
                    pack_name=pack_name,
                )
            )

    return CleanupRuleCatalog(tuple(packs), tuple(issues))


def _pack_from_data(
    data: dict,
    *,
    source: str,
    path: str,
) -> CleanupRulePack:
    unknown = sorted(set(data) - _PACK_FIELDS)
    if unknown:
        raise RulePackValidationError(
            "unknown pack field(s): " + ", ".join(unknown)
        )
    schema_version = _integer(data, "schema_version")
    if schema_version != RULE_PACK_SCHEMA_VERSION:
        raise RulePackValidationError(
            f"unsupported schema_version {schema_version}; "
            f"expected {RULE_PACK_SCHEMA_VERSION}"
        )
    name = _non_empty_string(data, "name")
    _validate_identifier(name, "pack name")
    version = _non_empty_string(data, "version")
    description = _non_empty_string(data, "description")
    default_enabled = data.get("default_enabled", True)
    if not isinstance(default_enabled, bool):
        raise RulePackValidationError("default_enabled must be a boolean")
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RulePackValidationError("rules must be a non-empty array of tables")
    rules: list[CleanupRule] = []
    names: set[str] = set()
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict):
            raise RulePackValidationError(f"rules[{index}] must be a table")
        rule = _rule_from_data(
            raw_rule,
            pack_name=name,
            pack_version=version,
            schema_version=schema_version,
            source=source,
            enabled=default_enabled,
        )
        if rule.name in names:
            raise RulePackValidationError(
                f"duplicate rule name '{rule.name}' in pack '{name}'"
            )
        names.add(rule.name)
        rules.append(rule)
    return CleanupRulePack(
        name=name,
        version=version,
        schema_version=schema_version,
        description=description,
        source=source,
        enabled=default_enabled,
        rules=tuple(rules),
        path=path,
    )


def _rule_from_data(
    data: dict,
    *,
    pack_name: str,
    pack_version: str,
    schema_version: int,
    source: str,
    enabled: bool,
) -> CleanupRule:
    unknown = sorted(set(data) - _RULE_FIELDS)
    if unknown:
        raise RulePackValidationError(
            f"rule has unknown field(s): {', '.join(unknown)}"
        )
    name = _non_empty_string(data, "name")
    _validate_identifier(name, "rule name")
    description = _non_empty_string(data, "description")
    patterns = _string_list(data, "patterns", required=True)
    parent_indicators = _string_list(data, "parent_indicators")
    path_context = _string_list(data, "path_context")
    category = _non_empty_string(data, "category")
    rebuild_hint = data.get("rebuild_hint")
    if rebuild_hint is not None and not isinstance(rebuild_hint, str):
        raise RulePackValidationError("rebuild_hint must be a string")
    min_age_days = data.get("min_age_days", 0)
    if not isinstance(min_age_days, int) or min_age_days < 0:
        raise RulePackValidationError("min_age_days must be a non-negative integer")
    confidence = data.get("confidence", 0.8)
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise RulePackValidationError("confidence must be between 0 and 1")
    try:
        risk = RiskLevel(data.get("risk", RiskLevel.SAFE.value))
    except ValueError as exc:
        raise RulePackValidationError(f"invalid risk: {data.get('risk')}") from exc
    try:
        default_action = CleanupRuleActionPolicy(
            data.get("default_action", CleanupRuleActionPolicy.SAFE.value)
        )
    except ValueError as exc:
        raise RulePackValidationError(
            f"invalid default_action: {data.get('default_action')}"
        ) from exc
    return CleanupRule(
        name=name,
        description=description,
        patterns=patterns,
        risk=risk,
        parent_indicators=parent_indicators,
        path_context=path_context,
        min_age_days=min_age_days,
        category=category,
        enabled=enabled,
        rebuild_hint=rebuild_hint,
        confidence=float(confidence),
        default_action=default_action,
        pack_name=pack_name,
        pack_version=pack_version,
        schema_version=schema_version,
        source=source,
    )


def _copy_rule(rule: CleanupRule, *, enabled: bool) -> CleanupRule:
    return CleanupRule(
        name=rule.name,
        description=rule.description,
        patterns=list(rule.patterns),
        risk=rule.risk,
        parent_indicators=list(rule.parent_indicators),
        path_context=list(rule.path_context),
        min_age_days=rule.min_age_days,
        category=rule.category,
        enabled=enabled,
        rebuild_hint=rule.rebuild_hint,
        confidence=rule.confidence,
        default_action=rule.default_action,
        pack_name=rule.pack_name,
        pack_version=rule.pack_version,
        schema_version=rule.schema_version,
        source=rule.source,
    )


def _non_empty_string(data: dict, field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise RulePackValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _integer(data: dict, field_name: str) -> int:
    value = data.get(field_name)
    if not isinstance(value, int):
        raise RulePackValidationError(f"{field_name} must be an integer")
    return value


def _string_list(
    data: dict,
    field_name: str,
    *,
    required: bool = False,
) -> list[str]:
    value = data.get(field_name, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise RulePackValidationError(f"{field_name} must be an array of strings")
    if required and not value:
        raise RulePackValidationError(f"{field_name} must not be empty")
    return list(value)


def _validate_identifier(value: str, label: str) -> None:
    if re.fullmatch(r"[a-z][a-z0-9_-]*", value) is None:
        raise RulePackValidationError(
            f"{label} must match [a-z][a-z0-9_-]*"
        )
