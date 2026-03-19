"""Scan cache — mtime-based invalidation."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fs_monitor.models.tree import FSNode


def _cache_dir() -> str:
    cache_base = os.environ.get(
        "XDG_CACHE_HOME", os.path.expanduser("~/.cache")
    )
    d = os.path.join(cache_base, "fsmonitor-cli")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_key(path: str) -> str:
    """Generate a safe filename from a path."""
    return path.replace("/", "_").replace("\\", "_").strip("_") or "root"


def _node_to_dict(node: FSNode) -> dict:
    return {
        "name": node.name,
        "path": node.path,
        "size": node.size,
        "own_size": node.own_size,
        "file_count": node.file_count,
        "dir_count": node.dir_count,
        "is_dir": node.is_dir,
        "mtime": node.mtime,
        "depth": node.depth,
        "error": node.error,
        "children": [_node_to_dict(c) for c in node.children],
    }


def _dict_to_node(d: dict) -> FSNode:
    return FSNode(
        name=d["name"],
        path=d["path"],
        size=d["size"],
        own_size=d["own_size"],
        file_count=d["file_count"],
        dir_count=d["dir_count"],
        is_dir=d["is_dir"],
        mtime=d["mtime"],
        depth=d["depth"],
        error=d.get("error"),
        children=[_dict_to_node(c) for c in d.get("children", [])],
    )


class ScanCache:
    """Cache scan results with mtime-based invalidation."""

    def __init__(self, cache_dir: str | None = None):
        self._dir = cache_dir or _cache_dir()

    def _path_for(self, scan_path: str) -> str:
        return os.path.join(self._dir, _cache_key(scan_path) + ".json")

    def get(self, scan_path: str) -> FSNode | None:
        """Load cached scan result if still valid."""
        cache_path = self._path_for(scan_path)
        if not os.path.exists(cache_path):
            return None

        try:
            with open(cache_path) as f:
                data = json.load(f)

            cached_mtime = data.get("root_mtime", 0)
            try:
                current_mtime = os.stat(scan_path).st_mtime
            except OSError:
                return None

            if current_mtime > cached_mtime:
                return None

            return _dict_to_node(data["tree"])
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def put(self, scan_path: str, root: FSNode) -> None:
        """Save scan result to cache."""
        cache_path = self._path_for(scan_path)
        data = {
            "root_mtime": root.mtime,
            "cached_at": time.time(),
            "tree": _node_to_dict(root),
        }
        try:
            with open(cache_path, "w") as f:
                json.dump(data, f)
        except OSError:
            pass

    def invalidate(self, scan_path: str) -> None:
        """Remove cached result for a path."""
        cache_path = self._path_for(scan_path)
        try:
            os.unlink(cache_path)
        except OSError:
            pass

    def clear(self) -> None:
        """Clear all cached results."""
        for f in os.listdir(self._dir):
            if f.endswith(".json"):
                try:
                    os.unlink(os.path.join(self._dir, f))
                except OSError:
                    pass
