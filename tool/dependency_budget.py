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

#: disktide's own optional scanner accelerator, which a platform wheel
#: carries and a pure wheel does not. The budget exists to keep *dependencies*
#: pure -- a dependency that grows a C extension is a dependency that stops
#: installing everywhere -- and this one file is neither a dependency nor a
#: requirement: `disktide/scanner/_scanfast_py.py` runs when it is absent.
_OWN_ACCELERATOR = ("disktide", "scanner")
_OWN_ACCELERATOR_STEM = "_scanfast"


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


def is_own_accelerator(path: Path) -> bool:
    """Whether an installed path is disktide's own optional extension."""
    parts = path.parts
    return (
        len(parts) >= 3
        and path.suffix.lower() in _NATIVE_SUFFIXES
        and parts[-3:-1] == _OWN_ACCELERATOR
        and parts[-1].split(".", 1)[0] == _OWN_ACCELERATOR_STEM
    )


def distribution_files(
    distributions: list[Distribution],
) -> tuple[set[Path], list[Path]]:
    """Return unique installed files and third-party native-extension members.

    disktide's own accelerator is counted in the installed size, like every
    other file, and left out of the native list: see `_OWN_ACCELERATOR`.
    """
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
            if path.suffix.lower() in _NATIVE_SUFFIXES and not is_own_accelerator(
                Path(str(relative))
            ):
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
