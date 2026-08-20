"""Runtime dependency closure and installed-size helpers."""

from __future__ import annotations

import re
from importlib.metadata import (
    Distribution,
    PackageNotFoundError,
    distribution,
    distributions,
)
from pathlib import Path


_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_NATIVE_SUFFIXES = {".so", ".pyd", ".dylib"}


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_distributions(project: str) -> list[Distribution]:
    """Return the installed runtime dependency closure for ``project``."""
    pending = [project]
    found: dict[str, Distribution] = {}
    while pending:
        requested = pending.pop()
        normalized = normalize_name(requested)
        if normalized in found:
            continue
        try:
            item = distribution(requested)
        except PackageNotFoundError:
            continue
        name = item.metadata.get("Name") or requested
        found[normalize_name(name)] = item
        for requirement in item.requires or []:
            if "extra ==" in requirement or "extra==" in requirement:
                continue
            match = _REQUIREMENT_NAME.match(requirement)
            if match:
                pending.append(match.group(1))
    return [found[name] for name in sorted(found)]


def installed_distributions() -> list[Distribution]:
    """Return all distributions in a clean minimal environment."""
    found: dict[str, Distribution] = {}
    for item in distributions():
        name = item.metadata.get("Name")
        if name:
            found[normalize_name(name)] = item
    return [found[name] for name in sorted(found)]


def distribution_files(
    distributions: list[Distribution],
) -> tuple[set[Path], list[Path]]:
    """Return unique installed files and native-extension members."""
    files: set[Path] = set()
    native: list[Path] = []
    for item in distributions:
        for relative in item.files or []:
            path = Path(item.locate_file(relative))
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            files.add(path)
            if path.suffix.lower() in _NATIVE_SUFFIXES:
                native.append(path)
    return files, native


def installed_size(files: set[Path]) -> int:
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total
