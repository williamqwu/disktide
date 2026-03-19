"""XDG-compliant configuration loading (TOML)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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


@dataclass
class UIConfig:
    color_theme: str = "default"
    default_sort: str = "size"
    default_viz: str = "treemap"
    show_hidden: bool = False


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

    if "ui" in data:
        ui = data["ui"]
        config.ui.color_theme = ui.get("color_theme", "default")
        config.ui.default_sort = ui.get("default_sort", "size")
        config.ui.default_viz = ui.get("default_viz", "treemap")
        config.ui.show_hidden = ui.get("show_hidden", False)

    return config
