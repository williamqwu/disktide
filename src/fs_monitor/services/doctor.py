"""Diagnostic report service for installation and platform capabilities."""

from __future__ import annotations

import json
import os
import platform
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fs_monitor import APP_NAME, LEGACY_STORAGE_NAMESPACE, __version__
from fs_monitor.collectors.platform import get_platform_adapter
from fs_monitor.collectors.platform.base import PlatformAdapter
from fs_monitor.config import AppConfig, load_config
from fs_monitor.extensions.capabilities import (
    Capability,
    CapabilityId,
    CapabilityStatus,
)
from fs_monitor.scanner.sysinfo import detect_cpu_count
from fs_monitor.storage.database import Database
from fs_monitor.storage.migrations import CURRENT_VERSION, get_version


DOCTOR_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Versioned, JSON-safe doctor payload."""

    application: dict[str, object]
    platform: dict[str, object]
    paths: dict[str, object]
    config: dict[str, object]
    database: dict[str, object]
    metrics: dict[str, object]
    capabilities: dict[str, object]
    optional_extras: dict[str, object]
    scan_policy: dict[str, object]
    schema_version: int = DOCTOR_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "application": self.application,
            "platform": self.platform,
            "paths": self.paths,
            "config": self.config,
            "database": self.database,
            "metrics": self.metrics,
            "capabilities": self.capabilities,
            "optional_extras": self.optional_extras,
            "scan_policy": self.scan_policy,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def build_doctor_report(
    *,
    adapter: PlatformAdapter | None = None,
    show_paths: bool = False,
    database_factory: Callable[[], Database] = Database,
    config_loader: Callable[..., AppConfig] = load_config,
) -> DoctorReport:
    """Build a complete report while isolating every optional probe."""
    platform_adapter = adapter or get_platform_adapter()
    capabilities = platform_adapter.capabilities("/")
    cpu_total, cpu_available = detect_cpu_count()
    memory_probe = _safe_memory_probe(platform_adapter)

    application_paths = _application_paths(show_paths)
    config_path = _actual_config_path()
    try:
        config = config_loader(config_path)
        config_status = "available"
        if config_path.exists():
            config_reason = "configuration loaded"
        else:
            config_reason = "configuration file does not exist; defaults are active"
    except Exception as exc:
        config = AppConfig()
        config_status = "unavailable"
        config_reason = f"configuration could not be loaded: {type(exc).__name__}: {exc}"

    database_report = _database_report(
        database_factory,
        show_paths=show_paths,
    )

    metric_items = {
        "logical": capabilities.get(CapabilityId.LOGICAL_METRIC).to_dict(),
        "allocated": capabilities.get(CapabilityId.ALLOCATED_METRIC).to_dict(),
        "unique": capabilities.get(CapabilityId.UNIQUE_METRIC).to_dict(),
        "files": Capability(
            CapabilityId.FILES_METRIC,
            CapabilityStatus.AVAILABLE,
            "file and symlink entry counts are platform independent",
        ).to_dict(),
    }
    platform_items = {
        capability_id.value: capabilities.get(capability_id).to_dict()
        for capability_id in (
            CapabilityId.MOUNT_ENUMERATION,
            CapabilityId.BLOCK_DEVICES,
            CapabilityId.STORAGE_MEDIUM,
            CapabilityId.TRASH,
            CapabilityId.FILESYSTEM_EVENTS,
        )
    }

    memory: dict[str, object] = {
        "status": memory_probe.status.value,
        "reason": memory_probe.reason,
        "total_mb": None,
        "available_mb": None,
    }
    if memory_probe.value is not None:
        memory["total_mb"] = memory_probe.value.total_mb
        memory["available_mb"] = memory_probe.value.available_mb

    return DoctorReport(
        application={
            "name": APP_NAME,
            "version": __version__,
            "python_version": platform.python_version(),
            "python_executable": Path(sys.executable).name,
            "textual_version": _distribution_version("textual"),
            "textual_plotext_version": _distribution_version("textual-plotext"),
            "core_install": "pure-python",
        },
        platform={
            "system": platform_adapter.system,
            "release": platform.release(),
            "machine": platform.machine() or "unknown",
            "adapter": platform_adapter.name,
            "cpu_total": cpu_total,
            "cpu_available": cpu_available,
            "memory": memory,
        },
        paths=application_paths,
        config={
            "status": config_status,
            "reason": _redact_text(config_reason, show_paths),
        },
        database=database_report,
        metrics=metric_items,
        capabilities=platform_items,
        optional_extras=_optional_extras(),
        scan_policy={
            "one_file_system": config.scan.one_file_system,
            "exclude_pseudo_filesystems": config.scan.exclude_pseudo_filesystems,
            "max_depth": config.scan.max_depth,
            "symlinks": "never-follow",
            "hardlinks": "lexical-owner",
        },
    )


def render_doctor_report(report: DoctorReport) -> str:
    """Render the default human-readable doctor output."""
    payload = report.to_dict()
    application = payload["application"]
    platform_info = payload["platform"]
    paths = payload["paths"]
    database = payload["database"]

    lines = [
        "fsmonitor doctor",
        "================",
        "",
        "Application",
        f"  Version: {application['version']}",
        f"  Python: {application['python_version']}",
        f"  Textual: {application['textual_version']}",
        f"  Textual Plotext: {application['textual_plotext_version']}",
        f"  Core install: {application['core_install']}",
        "",
        "Platform",
        f"  OS: {platform_info['system']} {platform_info['release']}",
        f"  Machine: {platform_info['machine']}",
        f"  Adapter: {platform_info['adapter']}",
        (
            f"  CPUs: {platform_info['cpu_available']} available / "
            f"{platform_info['cpu_total']} total"
        ),
        "",
        "Application paths",
        f"  Config: {paths['config']}",
        f"  Database: {paths['database']}",
        f"  Cache: {paths['cache']}",
        f"  Log: {paths['log']}",
        "  Paths: raw" if not paths["redacted"] else "  Paths: redacted (use --show-paths)",
        "",
        "Configuration",
        f"  [{str(payload['config']['status']).upper()}] {payload['config']['reason']}",
        "",
        "Database",
        f"  [{str(database['status']).upper()}] {database['reason']}",
        f"  Schema: {database['schema_version']} / {database['expected_schema_version']}",
        f"  Writable persistence: {database['writable']}",
        "",
        "Storage metrics",
    ]
    lines.extend(_render_capability_group(payload["metrics"]))
    lines.extend(["", "Platform capabilities"])
    lines.extend(_render_capability_group(payload["capabilities"]))
    lines.extend(["", "Optional extras"])
    lines.extend(_render_capability_group(payload["optional_extras"]))

    policy = payload["scan_policy"]
    lines.extend([
        "",
        "Default scan policy",
        f"  One filesystem: {policy['one_file_system']}",
        f"  Exclude pseudo filesystems: {policy['exclude_pseudo_filesystems']}",
        f"  Max depth: {policy['max_depth'] if policy['max_depth'] is not None else 'unlimited'}",
        f"  Symlinks: {policy['symlinks']}",
        f"  Hardlinks: {policy['hardlinks']}",
    ])
    return "\n".join(lines)


def _render_capability_group(items: object) -> list[str]:
    if not isinstance(items, dict):
        return []
    lines: list[str] = []
    labels = {
        "logical": "Logical",
        "allocated": "Allocated",
        "unique": "Unique",
        "files": "Files",
        "mount_enumeration": "Mount enumeration",
        "block_devices": "Block devices",
        "storage_medium": "Storage medium",
        "trash": "Trash/quarantine",
        "filesystem_events": "Filesystem events",
        "watch": "Watch",
        "remote": "Remote",
        "web": "Web",
        "export": "Export",
    }
    for key, value in items.items():
        if not isinstance(value, dict):
            continue
        status = str(value.get("status", "unavailable"))
        marker = {
            "available": "OK",
            "degraded": "WARN",
            "unavailable": "NO",
        }.get(status, "NO")
        label = labels.get(key, key.replace("_", " ").title())
        lines.append(f"  [{marker}] {label}: {value.get('reason', '')}")
        suggestion = value.get("suggestion")
        if suggestion:
            lines.append(f"       Suggestion: {suggestion}")
    return lines


def _database_report(
    database_factory: Callable[[], Database],
    *,
    show_paths: bool,
) -> dict[str, object]:
    database: Database | None = None
    try:
        database = database_factory()
        database.connect()
        schema_version = get_version(database.conn)
        read_only = bool(getattr(database, "read_only", False))
        if read_only:
            status = "read-only"
            reason = (
                database.degraded_reason
                or "persistent SQLite database is available read-only"
            )
            writable = False
        elif database.degraded:
            status = "degraded"
            reason = database.degraded_reason or "using an in-memory database"
            writable = False
        else:
            status = "available"
            reason = "persistent SQLite database is writable"
            writable = True
        path = _redact_path(Path(database.path), show_paths)
        return {
            "status": status,
            "reason": _redact_text(reason, show_paths),
            "path": path,
            "schema_version": schema_version,
            "expected_schema_version": CURRENT_VERSION,
            "writable": writable,
            "degraded": database.degraded,
            "read_only": read_only,
            "recovery_hint": _redact_text(
                getattr(database, "recovery_hint", None) or "",
                show_paths,
            ) or None,
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "reason": _redact_text(
                f"database probe failed: {type(exc).__name__}: {exc}",
                show_paths,
            ),
            "path": _application_paths(show_paths)["database"],
            "schema_version": None,
            "expected_schema_version": CURRENT_VERSION,
            "writable": False,
            "degraded": True,
            "read_only": False,
            "recovery_hint": None,
        }
    finally:
        if database is not None:
            try:
                database.close()
            except Exception:
                pass


def _safe_memory_probe(adapter: PlatformAdapter):
    try:
        return adapter.memory_info()
    except Exception as exc:
        from fs_monitor.collectors.platform.models import ProbeResult

        return ProbeResult.unavailable(
            f"memory probe failed: {type(exc).__name__}: {exc}"
        )


def _optional_extras() -> dict[str, object]:
    reason = "not shipped by the 0.2 core installation"
    suggestion = "No action required; this integration is reserved for a later wave."
    return {
        name: {
            "status": "unavailable",
            "available": False,
            "supported": False,
            "reason": reason,
            "suggestion": suggestion,
        }
        for name in ("watch", "remote", "web", "export")
    }


def _distribution_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "unavailable"


def _actual_config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / LEGACY_STORAGE_NAMESPACE / "config.toml"


def _application_paths(show_paths: bool) -> dict[str, object]:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    data_root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return {
        "config": _redact_path(
            config_root / LEGACY_STORAGE_NAMESPACE / "config.toml",
            show_paths,
        ),
        "database": _redact_path(
            data_root / LEGACY_STORAGE_NAMESPACE / "data.db",
            show_paths,
        ),
        "cache": _redact_path(
            cache_root / LEGACY_STORAGE_NAMESPACE,
            show_paths,
        ),
        "log": _redact_path(
            state_root / LEGACY_STORAGE_NAMESPACE / "fsmonitor.log",
            show_paths,
        ),
        "redacted": not show_paths,
    }


def _redact_path(path: Path, show_paths: bool) -> str:
    expanded = path.expanduser()
    if show_paths:
        return str(expanded)
    home = Path.home()
    try:
        return str(Path("~") / expanded.relative_to(home))
    except ValueError:
        pass
    roots = {
        "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME"),
        "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME"),
        "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME"),
        "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME"),
    }
    for variable, raw_root in roots.items():
        if not raw_root:
            continue
        try:
            relative = expanded.relative_to(Path(raw_root).expanduser())
        except ValueError:
            continue
        return str(Path(f"${variable}") / relative)
    return f"<redacted>/{expanded.name}"


def _redact_text(value: str, show_paths: bool) -> str:
    if show_paths:
        return value
    replacements = [str(Path.home())]
    replacements.extend(
        raw
        for raw in (
            os.environ.get("XDG_CONFIG_HOME"),
            os.environ.get("XDG_DATA_HOME"),
            os.environ.get("XDG_CACHE_HOME"),
            os.environ.get("XDG_STATE_HOME"),
        )
        if raw
    )
    redacted = value
    for root in replacements:
        redacted = redacted.replace(root, "<redacted>")
    return re.sub(
        r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|/)[^\s,;]+",
        "<redacted>",
        redacted,
    )
