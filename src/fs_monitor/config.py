"""XDG-compliant configuration loading (TOML)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_DURATION_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str) -> int:
    """Parse a duration string like '6h', '30m', '1d' into seconds.

    Also accepts plain integers (treated as seconds).
    """
    value = value.strip()
    unit = value[-1].lower()
    if unit in _DURATION_MULTIPLIERS:
        return int(value[:-1]) * _DURATION_MULTIPLIERS[unit]
    return int(value)


def format_duration(seconds: int) -> str:
    """Format seconds into a human-friendly duration string."""
    if seconds <= 0:
        return "0s"
    for unit, mult in [("d", 86400), ("h", 3600), ("m", 60)]:
        if seconds >= mult and seconds % mult == 0:
            return f"{seconds // mult}{unit}"
    return f"{seconds}s"


@dataclass
class ScanConfig:
    max_depth: int | None = None
    workers: int | None = None
    follow_symlinks: bool = False
    exclude_patterns: list[str] = field(default_factory=list)


@dataclass
class CleanupConfig:
    enabled_rules: list[str] = field(default_factory=list)
    disabled_rules: list[str] = field(default_factory=list)
    require_confirm_dangerous: bool = True


@dataclass
class MonitorConfig:
    default_interval: int = 21600  # 6 hours in seconds
    snapshot_retention: int = 30  # days
    max_watch_time: int | None = None  # seconds, None = unlimited
    strict_path: bool = False  # True = exact path match only


@dataclass
class UIConfig:
    color_theme: str = "warm"
    default_sort: str = "size"
    default_viz: str = "treemap"
    show_hidden: bool = False
    default_scan_path: str | None = None


@dataclass
class AppConfig:
    scan: ScanConfig = field(default_factory=ScanConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    ui: UIConfig = field(default_factory=UIConfig)


def _config_path() -> Path:
    config_dir = os.environ.get(
        "XDG_CONFIG_HOME", os.path.expanduser("~/.config")
    )
    return Path(config_dir) / "fsmonitor-cli" / "config.toml"


def save_config(config: AppConfig, path: str | Path | None = None) -> None:
    """Save configuration to TOML file."""
    config_file = Path(path) if path else _config_path()
    config_file.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []

    lines.append("[scan]")
    if config.scan.max_depth is not None:
        lines.append(f"max_depth = {config.scan.max_depth}")
    if config.scan.workers is not None:
        lines.append(f"workers = {config.scan.workers}")
    lines.append(f"follow_symlinks = {'true' if config.scan.follow_symlinks else 'false'}")
    if config.scan.exclude_patterns:
        patterns = ", ".join(f'"{p}"' for p in config.scan.exclude_patterns)
        lines.append(f"exclude_patterns = [{patterns}]")
    lines.append("")

    lines.append("[cleanup]")
    if config.cleanup.enabled_rules:
        rules = ", ".join(f'"{r}"' for r in config.cleanup.enabled_rules)
        lines.append(f"enabled_rules = [{rules}]")
    if config.cleanup.disabled_rules:
        rules = ", ".join(f'"{r}"' for r in config.cleanup.disabled_rules)
        lines.append(f"disabled_rules = [{rules}]")
    lines.append(f"require_confirm_dangerous = {'true' if config.cleanup.require_confirm_dangerous else 'false'}")
    lines.append("")

    lines.append("[monitor]")
    lines.append(f"default_interval = {config.monitor.default_interval}")
    lines.append(f"snapshot_retention = {config.monitor.snapshot_retention}")
    if config.monitor.max_watch_time is not None:
        lines.append(f"max_watch_time = {config.monitor.max_watch_time}")
    if config.monitor.strict_path:
        lines.append("strict_path = true")
    lines.append("")

    lines.append("[ui]")
    lines.append(f'color_theme = "{config.ui.color_theme}"')
    lines.append(f'default_sort = "{config.ui.default_sort}"')
    lines.append(f'default_viz = "{config.ui.default_viz}"')
    lines.append(f"show_hidden = {'true' if config.ui.show_hidden else 'false'}")
    if config.ui.default_scan_path is not None:
        lines.append(f'default_scan_path = "{config.ui.default_scan_path}"')
    lines.append("")

    config_file.write_text("\n".join(lines))


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load configuration from TOML file.

    Falls back to defaults if file doesn't exist.
    """
    config_file = Path(path) if path else _config_path()

    if not config_file.exists():
        return AppConfig()

    with open(config_file, "rb") as f:
        data = tomllib.load(f)

    config = AppConfig()

    if "scan" in data:
        scan = data["scan"]
        config.scan.max_depth = scan.get("max_depth")
        config.scan.workers = scan.get("workers")
        config.scan.follow_symlinks = scan.get("follow_symlinks", False)
        config.scan.exclude_patterns = scan.get("exclude_patterns", [])

    if "cleanup" in data:
        cleanup = data["cleanup"]
        config.cleanup.enabled_rules = cleanup.get("enabled_rules", [])
        config.cleanup.disabled_rules = cleanup.get("disabled_rules", [])
        config.cleanup.require_confirm_dangerous = cleanup.get(
            "require_confirm_dangerous", True
        )

    if "monitor" in data:
        monitor = data["monitor"]
        config.monitor.default_interval = monitor.get("default_interval", 21600)
        config.monitor.snapshot_retention = monitor.get("snapshot_retention", 30)
        config.monitor.max_watch_time = monitor.get("max_watch_time")
        config.monitor.strict_path = monitor.get("strict_path", False)

    if "ui" in data:
        ui = data["ui"]
        config.ui.color_theme = ui.get("color_theme", "default")
        config.ui.default_sort = ui.get("default_sort", "size")
        config.ui.default_viz = ui.get("default_viz", "treemap")
        config.ui.show_hidden = ui.get("show_hidden", False)
        config.ui.default_scan_path = ui.get("default_scan_path")

    return config
