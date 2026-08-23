"""Application paths with compatibility for pre-SizeTrail installations."""

from __future__ import annotations

import os
from pathlib import Path

from sizetrail import LEGACY_STORAGE_NAMESPACE, STORAGE_NAMESPACE


def _xdg_root(variable: str, fallback: str) -> Path:
    return Path(os.environ.get(variable, os.path.expanduser(fallback)))


def _compatible_root(
    base: Path,
    *,
    current_markers: tuple[str, ...] = (),
    legacy_markers: tuple[str, ...] = (),
) -> Path:
    current = base / STORAGE_NAMESPACE
    legacy = base / LEGACY_STORAGE_NAMESPACE
    if any((current / marker).exists() for marker in current_markers):
        return current
    if any((legacy / marker).exists() for marker in legacy_markers):
        return legacy
    if current.exists():
        return current
    if legacy.exists():
        return legacy
    return current


def config_root() -> Path:
    return _compatible_root(
        _xdg_root("XDG_CONFIG_HOME", "~/.config"),
        current_markers=("config.toml", "cleanup-rules"),
        legacy_markers=("config.toml", "cleanup-rules"),
    )


def data_root() -> Path:
    return _compatible_root(
        _xdg_root("XDG_DATA_HOME", "~/.local/share"),
        current_markers=("data.db",),
        legacy_markers=("data.db",),
    )


def cache_root() -> Path:
    return _compatible_root(_xdg_root("XDG_CACHE_HOME", "~/.cache"))


def state_root() -> Path:
    return _compatible_root(
        _xdg_root("XDG_STATE_HOME", "~/.local/state"),
        current_markers=("sizetrail.log",),
        legacy_markers=("fsmonitor.log",),
    )


def config_file() -> Path:
    return config_root() / "config.toml"


def database_file() -> Path:
    return data_root() / "data.db"


def state_log_file() -> Path:
    root = state_root()
    legacy_log = root / "fsmonitor.log"
    if root.name == LEGACY_STORAGE_NAMESPACE and legacy_log.exists():
        return legacy_log
    return root / "sizetrail.log"
