"""Compatibility facade for declarative cleanup rule packs."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from sizetrail.extensions.cleanup_rules import (
    CleanupRuleCatalog,
    load_rule_catalog,
)
from sizetrail.models.patterns import CleanupRule


def get_rule_catalog(
    *,
    disabled_packs: Iterable[str] = (),
    user_directory: str | Path | None = None,
) -> CleanupRuleCatalog:
    return load_rule_catalog(
        disabled_packs=disabled_packs,
        user_directory=user_directory,
    )


def get_rules(
    include_disabled: bool = False,
    *,
    disabled_packs: Iterable[str] = (),
    user_directory: str | Path | None = None,
) -> list[CleanupRule]:
    """Return declarative rules while preserving the Wave 01 API."""
    catalog = get_rule_catalog(
        disabled_packs=disabled_packs,
        user_directory=user_directory,
    )
    return list(catalog.all_rules if include_disabled else catalog.rules)


def get_rule_by_name(
    name: str,
    *,
    disabled_packs: Iterable[str] = (),
    user_directory: str | Path | None = None,
) -> CleanupRule | None:
    return get_rule_catalog(
        disabled_packs=disabled_packs,
        user_directory=user_directory,
    ).get_rule(name)


BUILTIN_RULES: list[CleanupRule] = get_rules(include_disabled=True)
