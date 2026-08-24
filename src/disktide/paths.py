"""Application paths with compatibility for pre-DiskTide installations."""

from __future__ import annotations

import os
from pathlib import Path

from disktide import (
    LEGACY_STORAGE_NAMESPACE,
    LEGACY_STORAGE_NAMESPACES,
    PREVIOUS_STORAGE_NAMESPACE,
    STORAGE_NAMESPACE,
)


_NAMESPACE_ORDER = (STORAGE_NAMESPACE, *LEGACY_STORAGE_NAMESPACES)
_STATE_LOG_NAMES = {
    STORAGE_NAMESPACE: "disktide.log",
    PREVIOUS_STORAGE_NAMESPACE: "sizetrail.log",
    LEGACY_STORAGE_NAMESPACE: "fsmonitor.log",
}


def _xdg_root(variable: str, fallback: str) -> Path:
    return Path(os.environ.get(variable, os.path.expanduser(fallback)))


def _compatible_root(
    base: Path,
    *,
    markers_by_namespace: dict[str, tuple[str, ...]] | None = None,
) -> Path:
    roots = [(namespace, base / namespace) for namespace in _NAMESPACE_ORDER]
    markers_by_namespace = markers_by_namespace or {}
    for namespace, root in roots:
        markers = markers_by_namespace.get(namespace, ())
        if any((root / marker).exists() for marker in markers):
            return root
    for _, root in roots:
        if root.exists():
            return root
    return roots[0][1]


def _shared_markers(*markers: str) -> dict[str, tuple[str, ...]]:
    return {namespace: markers for namespace in _NAMESPACE_ORDER}


def config_root() -> Path:
    return _compatible_root(
        _xdg_root("XDG_CONFIG_HOME", "~/.config"),
        markers_by_namespace=_shared_markers("config.toml", "cleanup-rules"),
    )


def data_root() -> Path:
    return _compatible_root(
        _xdg_root("XDG_DATA_HOME", "~/.local/share"),
        markers_by_namespace=_shared_markers("data.db"),
    )


def cache_root() -> Path:
    return _compatible_root(_xdg_root("XDG_CACHE_HOME", "~/.cache"))


def state_root() -> Path:
    return _compatible_root(
        _xdg_root("XDG_STATE_HOME", "~/.local/state"),
        markers_by_namespace={
            namespace: (log_name,)
            for namespace, log_name in _STATE_LOG_NAMES.items()
        },
    )


def config_file() -> Path:
    return config_root() / "config.toml"


def database_file() -> Path:
    return data_root() / "data.db"


def state_log_file() -> Path:
    root = state_root()
    legacy_log_name = _STATE_LOG_NAMES.get(root.name)
    if legacy_log_name is not None:
        legacy_log = root / legacy_log_name
        if legacy_log.exists():
            return legacy_log
    return root / "disktide.log"
