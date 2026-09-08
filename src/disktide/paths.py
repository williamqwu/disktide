"""Application paths with compatibility for pre-DiskTide installations."""

from __future__ import annotations

import os
from pathlib import Path

from disktide import (
    LEGACY_STORAGE_NAMESPACES,
    STORAGE_NAMESPACE,
)


_NAMESPACE_ORDER = (STORAGE_NAMESPACE, *LEGACY_STORAGE_NAMESPACES)


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


def config_file() -> Path:
    return config_root() / "config.toml"


def database_file() -> Path:
    return data_root() / "data.db"
