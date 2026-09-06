"""XDG-compliant configuration loading (TOML)."""

from __future__ import annotations

import re
import socket
import json
from dataclasses import dataclass, field
from pathlib import Path

from disktide._compat import tomllib
from disktide.keys import DEFAULT_PRESET as DEFAULT_KEY_PRESET
from disktide.paths import config_file, config_root
from disktide.themes import resolve_theme
# Pure stdlib, unlike the rest of `viz` — the reason `resolve_theme` had to
# be split out of `viz.colors` does not apply to it, so the vocabulary can
# live in one place instead of being restated here.
from disktide.viz.colordepth import normalize_depth
from disktide.viz.ringshape import DEFAULT_RING_SHAPE, resolve_ring_shape

#: The three visualisations the Settings picker offers. Textual's `Select`
#: makes a value outside its options fatal, so loader and picker share one
#: vocabulary rather than disagreeing about what a config file may say.
VIZ_CHOICES = ("treemap", "sunburst", "details")
DEFAULT_VIZ = "sunburst"

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


#: A count and an optional unit, and nothing else. Deliberately not signed:
#: a negative duration is not a duration, and every caller rejected it a step
#: later anyway.
_DURATION_PATTERN = re.compile(r"\d+[smhd]?", re.IGNORECASE)


def parse_duration(value: str) -> int:
    """Parse a duration string like '6h', '30m', '1d' into seconds.

    Also accepts plain integers (treated as seconds).

    A value that is not one says so in those terms. It used to fall through
    to `int()`, whose complaint is about an implementation detail and, for a
    unit this does not know, about a string the user never typed: `watch
    --interval abc` reported `invalid literal for int() with base 10: 'abc'`
    and `--interval 1ns` reported the same for `'1n'`. `compare --since` was
    the only caller that caught the exception and replaced the text, which is
    why it alone had a usable message.
    """
    text = value.strip() if isinstance(value, str) else value
    if not isinstance(text, str) or _DURATION_PATTERN.fullmatch(text) is None:
        raise ValueError("expected a duration such as 7d, 12h, or 30m")
    unit = text[-1].lower()
    if unit in _DURATION_MULTIPLIERS:
        return int(text[:-1]) * _DURATION_MULTIPLIERS[unit]
    return int(text)


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
    event_mode: str = "auto"


@dataclass
class CleanupConfig:
    prefer_trash: bool = True
    quarantine_retention_days: int = 7
    quarantine_max_bytes: int = 10 * 1024**3
    disabled_rule_packs: list[str] = field(default_factory=list)
    map_max_points: int = 80


@dataclass
class UIConfig:
    color_theme: str = "disktide"
    default_viz: str = "sunburst"
    show_cleanup: bool = False
    default_scan_path: str | None = None  # legacy flat field
    hostname_aware_paths: bool = True
    # When true, swap "fancy" Unicode characters (block-drawing bars,
    # accessibility glyphs) for ASCII fallbacks, and paint the charts
    # without block glyphs at all — the sunburst's disc becomes pure
    # background colour. Useful in web-based shells whose fonts don't
    # ship the full Unicode block range.
    safe_rendering: bool = False
    # Mouse reporting is negotiated once, when the Textual driver enters
    # application mode, so this is read at launch and cannot be flipped
    # mid-session. Turning it off hands clicks and drags back to the
    # terminal emulator, which is what a user wants when their terminal's
    # own selection/paste is more valuable than in-app hit testing.
    mouse: bool = True
    # Pixel height/width ratio of one character cell, or None for "measure
    # it". A manual value is the only thing that makes the sunburst round
    # in the terminals that report no pixel size at all — xterm.js web
    # shells, ConPTY, mosh, screen — where every automatic layer comes up
    # empty and the disc falls back to the historical 2.0.
    cell_aspect: float | None = None
    # "auto" | "truecolor" | "256" | "16": how many colours the terminal
    # is painted with. `auto` resolves it in layers (`viz.colordepth`),
    # which is right everywhere except a terminal that lies about itself
    # — an xterm.js web shell announces `xterm-16color` and can in fact
    # do RGB, and this is where a user says so once instead of exporting
    # a variable every login.
    color_depth: str = "auto"
    # "disc" | "fill" | "tiles": whether the ring chart's rings are
    # circles, rectangles stretched to the pane's own edges, or those same
    # rectangles cut into blocks by straight lines instead of by rays. A
    # terminal cell is a rectangle, so a rectangular ring lands its
    # silhouette, its hole and every ring boundary exactly on cell edges
    # where a circle can only be anti-aliased towards them; `tiles` goes
    # further and has no diagonal edge anywhere in the picture, which is
    # why it is the default (`viz.ringshape`).
    ring_shape: str = DEFAULT_RING_SHAPE
    # "auto" | "on" | "off". Controls whether the active viz tab (sunburst
    # or treemap) redraws live with partial scan data, vs. waiting for the
    # scan to finish and rendering once. `auto` enables it on a terminal
    # roomy enough to read the chart on; see `resolve_live_scan_render`.
    # The opt-out is still worth having: the paint shares the GIL with the
    # scan, and the duty cycle bounds that cost rather than removing it.
    live_scan_render: str = "auto"


@dataclass
class HostPaths:
    """Per-hostname path storage."""
    default_scan_path: str | None = None
    last_visited_path: str | None = None


@dataclass
class KeysConfig:
    """The `[keys]` table: a named preset plus per-binding overrides.

    `preset` picks one of `disktide.keys.PRESETS` — `spine` (the shipped
    layout), `safe` (the v0.2.30 keys with only the three consequence
    mismatches fixed) or `classic` (the v0.2.30 keys exactly). `overrides`
    maps a binding id to a key and is applied on top of the preset, so
    "classic, except one key" is a two-line config.
    """

    preset: str = DEFAULT_KEY_PRESET
    overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class AppConfig:
    scan: ScanConfig = field(default_factory=ScanConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    keys: KeysConfig = field(default_factory=KeysConfig)
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
    return config_file()


def config_path() -> Path:
    """Return the active config path without creating it."""
    return _config_path()


def cleanup_rule_directory() -> Path:
    """Return the user declarative rule-pack directory without creating it."""
    return config_root() / "cleanup-rules"


def _toml_str(value: str) -> str:
    """Quote a string so TOML reads back exactly what was written.

    A directory name may hold a quote or a backslash; writing it raw made
    the file unparseable and every later launch died in the loader.
    """
    return json.dumps(value, ensure_ascii=False)


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
    lines.append(f"event_mode = {_toml_str(config.monitor.event_mode)}")
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
    if config.cleanup.disabled_rule_packs:
        values = ", ".join(
            json.dumps(name)
            for name in sorted(set(config.cleanup.disabled_rule_packs))
        )
        lines.append(f"disabled_rule_packs = [{values}]")
    lines.append(f"map_max_points = {config.cleanup.map_max_points}")
    lines.append("")

    if config.keys.preset != DEFAULT_KEY_PRESET or config.keys.overrides:
        lines.append("[keys]")
        if config.keys.preset != DEFAULT_KEY_PRESET:
            lines.append(f"preset = {_toml_str(config.keys.preset)}")
        for binding_id in sorted(config.keys.overrides):
            key = config.keys.overrides[binding_id]
            lines.append(f"{json.dumps(binding_id)} = {json.dumps(key)}")
        lines.append("")

    lines.append("[ui]")
    lines.append(f"color_theme = {_toml_str(config.ui.color_theme)}")
    lines.append(f"default_viz = {_toml_str(config.ui.default_viz)}")
    if config.ui.show_cleanup:
        lines.append("show_cleanup = true")
    if config.ui.safe_rendering:
        lines.append("safe_rendering = true")
    if not config.ui.mouse:
        lines.append("mouse = false")
    if config.ui.cell_aspect is not None:
        # `:g` drops the trailing zeros the calibration keys never produce
        # anyway; the suffix keeps it a TOML float rather than an int.
        rendered = f"{config.ui.cell_aspect:g}"
        if "." not in rendered:
            rendered += ".0"
        lines.append(f"cell_aspect = {rendered}")
    if config.ui.ring_shape != DEFAULT_RING_SHAPE:
        lines.append(f"ring_shape = {_toml_str(config.ui.ring_shape)}")
    if config.ui.color_depth != "auto":
        lines.append(f"color_depth = {_toml_str(config.ui.color_depth)}")
    if config.ui.live_scan_render != "auto":
        lines.append(f"live_scan_render = {_toml_str(config.ui.live_scan_render)}")
    if config.ui.default_scan_path is not None:
        lines.append(f"default_scan_path = {_toml_str(config.ui.default_scan_path)}")
    if not config.ui.hostname_aware_paths:
        lines.append("hostname_aware_paths = false")
    lines.append("")

    # Per-hostname path storage
    for hostname, hp in config.host_paths.items():
        lines.append(f"[paths.{_toml_str(hostname)}]")
        if hp.default_scan_path is not None:
            lines.append(f"default_scan_path = {_toml_str(hp.default_scan_path)}")
        if hp.last_visited_path is not None:
            lines.append(f"last_visited_path = {_toml_str(hp.last_visited_path)}")
        lines.append("")

    config_file.write_text("\n".join(lines))


@dataclass(slots=True)
class ScanOverrides:
    """Scan settings that belong to one launch rather than to the config.

    `-d`, `-w`, `--one-file-system` and `--exclude-pseudo` used to be
    written into the loaded `AppConfig` before the app was built, and the
    app saves that config -- on quit, and again whenever a path is
    remembered -- so a single `disktide -w 2 /srv` left `workers = 2` in
    `config.toml` for every later run, including plain `disktide scan`.
    `None` here means "not given on this launch"; the config's own value
    is what applies then.
    """

    max_depth: int | None = None
    workers: int | None = None
    one_file_system: bool | None = None
    exclude_pseudo_filesystems: bool | None = None

    def __bool__(self) -> bool:
        return any(
            value is not None
            for value in (
                self.max_depth,
                self.workers,
                self.one_file_system,
                self.exclude_pseudo_filesystems,
            )
        )


def _parse_cell_aspect(value: object) -> float | None:
    """Read a manual cell aspect out of a config file, or None.

    Anything that isn't a positive number — the literal string "auto" a
    user might reasonably write to mean "go back to measuring", a typo, a
    zero — is treated as unset rather than as an error, because the
    fallback is the automatic detection the field exists to override and
    refusing to start over a bad number would be far worse than ignoring
    it. `bool` is excluded explicitly: it is an `int` subclass, and
    `cell_aspect = true` should not resolve to 1.0.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if numeric != numeric or numeric <= 0.0:  # NaN, zero, negative
        return None
    from disktide.viz.cellgeom import clamp_cell_aspect

    return clamp_cell_aspect(numeric)


class ConfigError(ValueError):
    """A config file this build cannot read as written.

    Raised only for a value whose *type* is wrong, never for one whose
    meaning this build does not recognise: an unknown theme, ring shape or
    key binding still falls back to the default, because a file written by
    a newer release has to keep opening the app. A string where a number
    belongs has no such fallback -- it used to be copied into a monitor's
    stored policy, where it outlived the config edit that made it -- so it
    is reported where it is written, naming the section and the key.
    """


def _wrong_type(section: str, key: str, value: object, expected: str):
    return ConfigError(f"[{section}] {key}: expected {expected}, got {value!r}")


def _table(data: dict, name: str) -> dict:
    """One `[section]` of the file, or `{}` when there is not one."""
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            f"[{name}]: expected a table of settings, got {value!r}"
        )
    return value


def _int_value(
    table: dict, section: str, key: str, default, *, allow_none: bool = False
):
    """A whole number. `bool` is excluded: it is an `int` subclass."""
    if key not in table:
        return default
    value = table[key]
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _wrong_type(
            section, key, value, "a whole number written without quotes"
        )
    return value


def _bool_value(table: dict, section: str, key: str, default: bool) -> bool:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, bool):
        raise _wrong_type(section, key, value, "true or false")
    return value


def _str_value(table: dict, section: str, key: str, default=None):
    if key not in table:
        return default
    value = table[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise _wrong_type(section, key, value, "a quoted string")
    return value


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load configuration from TOML file.

    Falls back to defaults if the file doesn't exist. Raises `ConfigError`
    for a file that exists but cannot be read as written -- malformed TOML,
    or a value of the wrong type -- naming the section and key so the reader
    knows which line to fix.
    """
    config_file = Path(path) if path else _config_path()

    if not config_file.exists():
        return AppConfig()

    try:
        with open(config_file, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_file}: {exc}") from exc

    config = AppConfig()

    scan = _table(data, "scan")
    if scan:
        config.scan.max_depth = _int_value(
            scan, "scan", "max_depth", None, allow_none=True
        )
        config.scan.workers = _int_value(
            scan, "scan", "workers", None, allow_none=True
        )
        config.scan.one_file_system = _bool_value(
            scan, "scan", "one_file_system", False
        )
        config.scan.exclude_pseudo_filesystems = _bool_value(
            scan, "scan", "exclude_pseudo_filesystems", True
        )

    monitor = _table(data, "monitor")
    if monitor:
        config.monitor.default_interval = _int_value(
            monitor, "monitor", "default_interval", 21600
        )
        config.monitor.max_watch_time = _int_value(
            monitor, "monitor", "max_watch_time", None, allow_none=True
        )
        soft_budget = _int_value(
            monitor,
            "monitor",
            "database_soft_budget",
            2 * 1024**3,
            allow_none=True,
        )
        hard_budget = _int_value(
            monitor,
            "monitor",
            "database_hard_budget",
            3 * 1024**3,
            allow_none=True,
        )
        config.monitor.database_soft_budget = (
            None if soft_budget is None or soft_budget <= 0 else soft_budget
        )
        config.monitor.database_hard_budget = (
            None if hard_budget is None or hard_budget <= 0 else hard_budget
        )
        config.monitor.auto_start_in_tui = _bool_value(
            monitor, "monitor", "auto_start_in_tui", False
        )
        raw_event_mode = str(monitor.get("event_mode", "auto")).lower()
        config.monitor.event_mode = (
            raw_event_mode
            if raw_event_mode in {"auto", "events", "periodic"}
            else "auto"
        )

    cleanup = _table(data, "cleanup")
    if cleanup:
        config.cleanup.prefer_trash = _bool_value(
            cleanup, "cleanup", "prefer_trash", True
        )
        config.cleanup.quarantine_retention_days = max(
            1,
            _int_value(cleanup, "cleanup", "quarantine_retention_days", 7),
        )
        config.cleanup.quarantine_max_bytes = max(
            1,
            _int_value(
                cleanup, "cleanup", "quarantine_max_bytes", 10 * 1024**3
            ),
        )
        disabled = cleanup.get("disabled_rule_packs", [])
        if isinstance(disabled, list):
            config.cleanup.disabled_rule_packs = sorted(
                {
                    str(name)
                    for name in disabled
                    if isinstance(name, str) and name
                }
            )
        config.cleanup.map_max_points = min(
            500,
            max(10, _int_value(cleanup, "cleanup", "map_max_points", 80)),
        )

    ui = _table(data, "ui")
    if ui:
        # `warm`, `default` and `vivid` are all retired keys for what is
        # now `disktide`; `resolve_theme` owns that mapping so the loader,
        # the picker and the renderer cannot disagree. An unknown name
        # (hand-typed, or from a newer build) lands on the default rather
        # than raising when the picker tries to show it.
        config.ui.color_theme = resolve_theme(ui.get("color_theme"))
        raw_viz = str(ui.get("default_viz", DEFAULT_VIZ)).lower()
        # The Settings picker offers exactly these three and Textual makes
        # any other value fatal inside `compose`, so a hand-typed name has
        # to land on the default here rather than at the first `,`.
        config.ui.default_viz = (
            raw_viz if raw_viz in VIZ_CHOICES else DEFAULT_VIZ
        )
        config.ui.show_cleanup = _bool_value(ui, "ui", "show_cleanup", False)
        config.ui.safe_rendering = _bool_value(
            ui, "ui", "safe_rendering", False
        )
        config.ui.mouse = _bool_value(ui, "ui", "mouse", True)
        config.ui.cell_aspect = _parse_cell_aspect(ui.get("cell_aspect"))
        config.ui.ring_shape = resolve_ring_shape(ui.get("ring_shape"))
        # An unusable value reads as `auto` rather than as an error: the
        # fallback is the detection the key exists to override, and a
        # config file is not a place to fail a launch over a typo.
        config.ui.color_depth = (
            normalize_depth(ui.get("color_depth")) or "auto"
        )
        raw_live = ui.get("live_scan_render", "auto")
        config.ui.live_scan_render = (
            raw_live if raw_live in ("auto", "on", "off") else "auto"
        )
        config.ui.default_scan_path = _str_value(
            ui, "ui", "default_scan_path"
        )
        config.ui.hostname_aware_paths = _bool_value(
            ui, "ui", "hostname_aware_paths", True
        )

    if "keys" in data:
        keys = data["keys"]
        if isinstance(keys, dict):
            config.keys.preset = str(keys.get("preset", DEFAULT_KEY_PRESET))
            # Everything that is not `preset` is a binding id. Validation
            # belongs to `disktide.keys.resolve_keymap`, which reports an
            # unknown id as a toast instead of failing the load — a config
            # written against a newer release must still open the app.
            config.keys.overrides = {
                str(name): str(key)
                for name, key in keys.items()
                if name != "preset" and isinstance(key, str)
            }

    for hostname, hp_data in _table(data, "paths").items():
        # A stray `host = "x"` at the top of [paths] is skipped rather than
        # refused, the way an unknown [keys] entry is: the block is written
        # by disktide itself and a hand-edit here costs nothing but itself.
        if not isinstance(hp_data, dict):
            continue
        where = f"paths.{hostname}"
        config.host_paths[hostname] = HostPaths(
            default_scan_path=_str_value(hp_data, where, "default_scan_path"),
            last_visited_path=_str_value(hp_data, where, "last_visited_path"),
        )

    return config


# Auto-gate thresholds for the live-scan-render setting. The progress
# overlay docks into the left tree panel during a scan and the viz fills
# the right; below ~80 columns or ~24 rows the explorer's 40/60 split
# leaves neither side roomy enough for the chart to be legible, which is
# the whole of what this gate is for.
#
# It used to also require four cores, on the premise that spare cores
# hide the redraw. They do not: the render is Python and holds the GIL for
# every millisecond of a frame, so a live paint and a scan thread cannot
# run at once however many cores the host has -- more cores only meant
# more threads queueing behind the painter. The gate was in fact
# backwards, since a machine with sixteen cores is usually the one with
# the 300-column terminal, and frame cost grows with cells: 33 ms at
# 70x30 against 163 ms at 182x62. What bounds the cost is
# `ExplorerScreen`'s duty cycle, which forwards one frame per several
# times its own measured cost; the size below is a legibility floor.
_LIVE_RENDER_MIN_COLS = 80
_LIVE_RENDER_MIN_ROWS = 24


def resolve_live_scan_render(
    value: str,
    *,
    terminal_width: int | None = None,
    terminal_height: int | None = None,
    cpu_count: int | None = None,
) -> bool:
    """Resolve a `live_scan_render` config value to a concrete on/off.

    Explicit "on" / "off" honor the user; "auto" (the default) enables
    live rendering when the terminal is roomy enough for the chart to be
    worth looking at around the progress overlay. It is a legibility
    check and nothing more: what keeps the paint from starving the scan
    is `ExplorerScreen`'s duty cycle, not the host. `cpu_count` is still
    accepted, and ignored, because callers (and tests) pass it.

    The keyword arguments are taken from the runtime by default; tests
    inject them to assert the gate.
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
    return (
        terminal_width >= _LIVE_RENDER_MIN_COLS
        and terminal_height >= _LIVE_RENDER_MIN_ROWS
    )
