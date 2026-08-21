"""XDG-compliant configuration loading (TOML)."""

from __future__ import annotations

import os
import re
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from fs_monitor import LEGACY_STORAGE_NAMESPACE

_DURATION_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_SIZE_MULTIPLIERS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


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


def parse_size(value: str) -> int:
    """Parse byte sizes such as ``4MiB``, ``10GB``, or plain integers."""
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?i?b)?\s*",
        value,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ValueError(f"invalid size: {value}")
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    return int(number * _SIZE_MULTIPLIERS[unit])


@dataclass
class ScanConfig:
    max_depth: int | None = None
    workers: int | None = None
    one_file_system: bool = False
    exclude_pseudo_filesystems: bool = True


@dataclass
class MonitorConfig:
    default_interval: int = 21600  # 6 hours in seconds
    max_watch_time: int | None = None  # seconds, None = unlimited
    database_soft_budget: int | None = 2 * 1024**3
    database_hard_budget: int | None = 3 * 1024**3
    auto_start_in_tui: bool = False


@dataclass
class CleanupConfig:
    prefer_trash: bool = True
    quarantine_retention_days: int = 7
    quarantine_max_bytes: int = 10 * 1024**3


@dataclass
class UIConfig:
    color_theme: str = "warm"
    default_viz: str = "sunburst"
    show_cleanup: bool = False
    default_scan_path: str | None = None  # legacy flat field
    hostname_aware_paths: bool = True
    # When true, swap "fancy" Unicode characters (block-drawing bars,
    # accessibility glyphs) for ASCII fallbacks. Useful in web-based
    # shells whose fonts don't ship the full Unicode block range.
    safe_rendering: bool = False
    # "auto" | "on" | "off". Controls whether the active viz tab (sunburst
    # or treemap) redraws live with partial scan data, vs. waiting for the
    # scan to finish and rendering once. `auto` enables it on a roomy
    # terminal with enough cores; see `resolve_live_scan_render` for the
    # exact gate. The opt-out matters on cramped terminals and small VMs
    # where the per-frame redraw cost is noticeable against the scan.
    live_scan_render: str = "auto"


@dataclass
class HostPaths:
    """Per-hostname path storage."""
    default_scan_path: str | None = None
    last_visited_path: str | None = None


@dataclass
class AppConfig:
    scan: ScanConfig = field(default_factory=ScanConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    host_paths: dict[str, HostPaths] = field(default_factory=dict)


def current_hostname() -> str:
    """Return the current machine's hostname."""
    return socket.gethostname()


def get_effective_paths(config: AppConfig) -> HostPaths:
    """Return the HostPaths for the current host (or legacy fallback)."""
    if config.ui.hostname_aware_paths:
        key = current_hostname()
    else:
        key = "_default"

    hp = config.host_paths.get(key, HostPaths())

    # Legacy migration: if no per-host default but legacy field exists, use it
    if hp.default_scan_path is None and config.ui.default_scan_path is not None:
        hp.default_scan_path = config.ui.default_scan_path

    return hp


def set_effective_paths(config: AppConfig, paths: HostPaths) -> None:
    """Write back HostPaths for the current host."""
    if config.ui.hostname_aware_paths:
        key = current_hostname()
    else:
        key = "_default"

    config.host_paths[key] = paths
    # Keep legacy field in sync for backward compat
    config.ui.default_scan_path = paths.default_scan_path


def _config_path() -> Path:
    config_dir = os.environ.get(
        "XDG_CONFIG_HOME", os.path.expanduser("~/.config")
    )
    return Path(config_dir) / LEGACY_STORAGE_NAMESPACE / "config.toml"


def config_path() -> Path:
    """Return the active config path without creating it."""
    return _config_path()


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
    if config.scan.one_file_system:
        lines.append("one_file_system = true")
    if not config.scan.exclude_pseudo_filesystems:
        lines.append("exclude_pseudo_filesystems = false")
    lines.append("")

    lines.append("[monitor]")
    lines.append(f"default_interval = {config.monitor.default_interval}")
    if config.monitor.max_watch_time is not None:
        lines.append(f"max_watch_time = {config.monitor.max_watch_time}")
    lines.append(
        "database_soft_budget = "
        f"{config.monitor.database_soft_budget or 0}"
    )
    lines.append(
        "database_hard_budget = "
        f"{config.monitor.database_hard_budget or 0}"
    )
    if config.monitor.auto_start_in_tui:
        lines.append("auto_start_in_tui = true")
    lines.append("")

    lines.append("[cleanup]")
    if not config.cleanup.prefer_trash:
        lines.append("prefer_trash = false")
    lines.append(
        "quarantine_retention_days = "
        f"{config.cleanup.quarantine_retention_days}"
    )
    lines.append(
        "quarantine_max_bytes = "
        f"{config.cleanup.quarantine_max_bytes}"
    )
    lines.append("")

    lines.append("[ui]")
    lines.append(f'color_theme = "{config.ui.color_theme}"')
    lines.append(f'default_viz = "{config.ui.default_viz}"')
    if config.ui.show_cleanup:
        lines.append("show_cleanup = true")
    if config.ui.safe_rendering:
        lines.append("safe_rendering = true")
    if config.ui.live_scan_render != "auto":
        lines.append(f'live_scan_render = "{config.ui.live_scan_render}"')
    if config.ui.default_scan_path is not None:
        lines.append(f'default_scan_path = "{config.ui.default_scan_path}"')
    if not config.ui.hostname_aware_paths:
        lines.append("hostname_aware_paths = false")
    lines.append("")

    # Per-hostname path storage
    for hostname, hp in config.host_paths.items():
        lines.append(f'[paths."{hostname}"]')
        if hp.default_scan_path is not None:
            lines.append(f'default_scan_path = "{hp.default_scan_path}"')
        if hp.last_visited_path is not None:
            lines.append(f'last_visited_path = "{hp.last_visited_path}"')
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
        config.scan.one_file_system = scan.get("one_file_system", False)
        config.scan.exclude_pseudo_filesystems = scan.get(
            "exclude_pseudo_filesystems", True
        )

    if "monitor" in data:
        monitor = data["monitor"]
        config.monitor.default_interval = monitor.get("default_interval", 21600)
        config.monitor.max_watch_time = monitor.get("max_watch_time")
        soft_budget = monitor.get("database_soft_budget", 2 * 1024**3)
        hard_budget = monitor.get("database_hard_budget", 3 * 1024**3)
        config.monitor.database_soft_budget = (
            None if soft_budget is None or soft_budget <= 0 else soft_budget
        )
        config.monitor.database_hard_budget = (
            None if hard_budget is None or hard_budget <= 0 else hard_budget
        )
        config.monitor.auto_start_in_tui = monitor.get(
            "auto_start_in_tui", False
        )

    if "cleanup" in data:
        cleanup = data["cleanup"]
        config.cleanup.prefer_trash = cleanup.get("prefer_trash", True)
        config.cleanup.quarantine_retention_days = max(
            1,
            int(cleanup.get("quarantine_retention_days", 7)),
        )
        quarantine_max = cleanup.get("quarantine_max_bytes", 10 * 1024**3)
        config.cleanup.quarantine_max_bytes = max(1, int(quarantine_max))

    if "ui" in data:
        ui = data["ui"]
        config.ui.color_theme = ui.get("color_theme", "warm")
        config.ui.default_viz = ui.get("default_viz", "sunburst")
        config.ui.show_cleanup = ui.get("show_cleanup", False)
        config.ui.safe_rendering = ui.get("safe_rendering", False)
        raw_live = ui.get("live_scan_render", "auto")
        config.ui.live_scan_render = (
            raw_live if raw_live in ("auto", "on", "off") else "auto"
        )
        config.ui.default_scan_path = ui.get("default_scan_path")
        config.ui.hostname_aware_paths = ui.get("hostname_aware_paths", True)

    if "paths" in data:
        for hostname, hp_data in data["paths"].items():
            config.host_paths[hostname] = HostPaths(
                default_scan_path=hp_data.get("default_scan_path"),
                last_visited_path=hp_data.get("last_visited_path"),
            )

    return config


# Auto-gate thresholds for the live-scan-render setting. The progress
# overlay docks into the left tree panel during a scan and the viz fills
# the right; below ~80 columns or ~24 rows the explorer's 40/60 split
# leaves neither side roomy enough for the live render to be worth its
# per-frame cost. Four cores is the rough line at which a 50-200 ms
# braille fill per second stops competing with the scan worker threads
# for CPU.
_LIVE_RENDER_MIN_COLS = 80
_LIVE_RENDER_MIN_ROWS = 24
_LIVE_RENDER_MIN_CPUS = 4


def resolve_live_scan_render(
    value: str,
    *,
    terminal_width: int | None = None,
    terminal_height: int | None = None,
    cpu_count: int | None = None,
) -> bool:
    """Resolve a `live_scan_render` config value to a concrete on/off.

    Explicit "on" / "off" honor the user; "auto" (the default) enables
    live rendering only when both the terminal is roomy enough for the
    sunburst to be visible around the progress overlay AND there are
    enough cores that the per-frame redraw cost is not visible against
    the scan. The keyword arguments are taken from the runtime by
    default; tests inject them to assert the gate.
    """
    normalized = (value or "auto").strip().lower()
    if normalized == "on":
        return True
    if normalized == "off":
        return False
    # Anything else (including the documented "auto") falls through to
    # the heuristic so an unexpected value can't permanently disable a
    # feature the user can no longer enable from the dropdown.
    if terminal_width is None or terminal_height is None:
        import shutil
        try:
            size = shutil.get_terminal_size((_LIVE_RENDER_MIN_COLS, _LIVE_RENDER_MIN_ROWS))
        except OSError:
            size = None
        if size is not None:
            if terminal_width is None:
                terminal_width = size.columns
            if terminal_height is None:
                terminal_height = size.lines
    if terminal_width is None:
        terminal_width = _LIVE_RENDER_MIN_COLS
    if terminal_height is None:
        terminal_height = _LIVE_RENDER_MIN_ROWS
    if cpu_count is None:
        cpu_count = os.cpu_count() or 1
    return (
        terminal_width >= _LIVE_RENDER_MIN_COLS
        and terminal_height >= _LIVE_RENDER_MIN_ROWS
        and cpu_count >= _LIVE_RENDER_MIN_CPUS
    )
