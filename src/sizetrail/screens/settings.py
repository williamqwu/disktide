"""Configuration screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll, Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, Switch, Label, Input, Select

from sizetrail.config import (
    AppConfig,
    cleanup_rule_directory,
    current_hostname,
    format_duration,
    parse_duration,
    parse_size,
    save_config,
)
from sizetrail.cleanup.rules import get_rule_catalog
from sizetrail.models.patterns import RiskLevel
from sizetrail.scanner.sysinfo import storage_class
from sizetrail.repositories.snapshots import SnapshotRepository
from sizetrail.viz.colors import SCHEMES, set_color_scheme


class SettingsScreen(Screen):
    """Configuration settings screen."""

    BINDINGS = [
        Binding("escape", "dismiss_settings", "Back", show=True),
        # Arrow keys move focus between fields, like Tab/Shift+Tab.
        # `priority=True` so Select/Input widgets don't swallow them
        # when they have no useful arrow-key behaviour of their own
        # (Inputs use Home/End and ←/→ for caret movement; Selects
        # only need ↑/↓ when the dropdown is open).
        Binding("down", "focus_next_field", "Down", show=True, priority=True),
        Binding("up", "focus_previous_field", "Up", show=True, priority=True),
    ]

    DEFAULT_CSS = """
    SettingsScreen {
        layout: vertical;
    }

    .setting-row {
        height: 3;
        padding: 0 2;
    }

    .setting-label {
        width: 30;
    }

    .input-hint {
        width: 30;
        color: $text-muted;
    }

    .setting-input {
        width: 16;
    }

    .viz-select {
        width: 20;
    }

    #settings-container {
        padding: 1 2;
        height: 1fr;
    }

    .sysinfo-value {
        padding: 0 2;
        height: 1;
    }
    """

    def __init__(
        self,
        config: AppConfig,
        repository: SnapshotRepository | None = None,
        scan_path: str = "/",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._config = config
        self._repository = repository
        self._scan_path = scan_path
        self._system_info = None
        self._rule_catalog = get_rule_catalog(
            disabled_packs=self._config.cleanup.disabled_rule_packs,
            user_directory=cleanup_rule_directory(),
        )

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="settings-container"):
            yield Static("Settings", classes="title")
            yield Static("")

            yield Static("System Information", classes="section-title")
            yield Static("  Detecting...", id="sysinfo-cpus", classes="sysinfo-value")
            yield Static("", id="sysinfo-memory", classes="sysinfo-value")
            yield Static("", id="sysinfo-load", classes="sysinfo-value")
            yield Static("", id="sysinfo-storage", classes="sysinfo-value")
            yield Static("", id="sysinfo-recommendation", classes="sysinfo-value")
            yield Static("", id="sysinfo-dbsize", classes="sysinfo-value")

            yield Static("")
            yield Static("Scan Performance", classes="section-title")
            with Horizontal(classes="setting-row"):
                yield Label("Workers", classes="setting-label")
                yield Input(
                    placeholder="auto",
                    id="workers-input",
                    classes="setting-input",
                )
                yield Label("", id="workers-hint", classes="input-hint")

            with Horizontal(classes="setting-row"):
                yield Label("Max depth", classes="setting-label")
                yield Input(
                    placeholder="unlimited",
                    id="max-depth-input",
                    classes="setting-input",
                )
                yield Label("(blank = unlimited)", classes="input-hint")

            with Horizontal(classes="setting-row"):
                yield Label("One filesystem", classes="setting-label")
                yield Switch(
                    value=self._config.scan.one_file_system,
                    id="one-file-system",
                )

            with Horizontal(classes="setting-row"):
                yield Label("Exclude pseudo filesystems", classes="setting-label")
                yield Switch(
                    value=self._config.scan.exclude_pseudo_filesystems,
                    id="exclude-pseudo-filesystems",
                )

            yield Static("")
            yield Static("Cleanup Settings", classes="section-title")
            with Horizontal(classes="setting-row"):
                yield Label("Show Cleanup mode", classes="setting-label")
                yield Switch(value=self._config.ui.show_cleanup, id="show-cleanup")
            yield Static(
                "  Cleanup defaults to plan preview and recoverable Trash/quarantine.",
                classes="sysinfo-value",
            )
            yield Static("  Declarative rule packs", classes="sysinfo-value")
            risk_order = {
                RiskLevel.SAFE: 0,
                RiskLevel.MODERATE: 1,
                RiskLevel.DANGEROUS: 2,
            }
            for pack in self._rule_catalog.packs:
                maximum = max(
                    (rule.risk for rule in pack.rules),
                    key=lambda risk: risk_order[risk],
                    default=RiskLevel.SAFE,
                )
                with Horizontal(classes="setting-row"):
                    yield Label(
                        f"{pack.name} v{pack.version}",
                        classes="setting-label",
                    )
                    yield Switch(
                        value=pack.enabled,
                        id=f"cleanup-pack-{pack.name}",
                    )
                    yield Label(
                        f"{len(pack.rules)} rules · {pack.source} · max {maximum.value}",
                        classes="input-hint",
                    )
            if self._rule_catalog.issues:
                yield Static(
                    "  "
                    + "; ".join(
                        f"isolated {issue.path}: {issue.error}"
                        for issue in self._rule_catalog.issues
                    ),
                    classes="sysinfo-value",
                )

            yield Static("")
            yield Static("Monitor Settings", classes="section-title")
            with Horizontal(classes="setting-row"):
                yield Label("Default interval", classes="setting-label")
                yield Input(
                    value=format_duration(self._config.monitor.default_interval),
                    id="interval-input",
                    classes="setting-input",
                )
                yield Label("(e.g., 6h, 30m, 1d)", classes="input-hint")

            with Horizontal(classes="setting-row"):
                yield Label("Max watch time", classes="setting-label")
                yield Input(
                    placeholder="unlimited",
                    id="max-watch-time-input",
                    classes="setting-input",
                )
                yield Label("(e.g., 2h, 1d; blank = unlimited)", classes="input-hint")

            with Horizontal(classes="setting-row"):
                yield Label("Filesystem events", classes="setting-label")
                yield Select(
                    [
                        ("Auto", "auto"),
                        ("Require events", "events"),
                        ("Periodic only", "periodic"),
                    ],
                    value=self._config.monitor.event_mode,
                    id="monitor-event-mode",
                    classes="viz-select",
                    allow_blank=False,
                )
                yield Label("([watch] extra on Linux)", classes="input-hint")

            with Horizontal(classes="setting-row"):
                yield Label("Database soft budget", classes="setting-label")
                yield Input(
                    value=(
                        str(self._config.monitor.database_soft_budget)
                        if self._config.monitor.database_soft_budget is not None
                        else ""
                    ),
                    placeholder="unlimited",
                    id="monitor-soft-budget",
                    classes="setting-input",
                )
                yield Label("(bytes or 2GiB)", classes="input-hint")

            with Horizontal(classes="setting-row"):
                yield Label("Database hard budget", classes="setting-label")
                yield Input(
                    value=(
                        str(self._config.monitor.database_hard_budget)
                        if self._config.monitor.database_hard_budget is not None
                        else ""
                    ),
                    placeholder="unlimited",
                    id="monitor-hard-budget",
                    classes="setting-input",
                )
                yield Label("(blocks new snapshots)", classes="input-hint")

            with Horizontal(classes="setting-row"):
                yield Label("Auto-start TUI session", classes="setting-label")
                yield Switch(
                    value=self._config.monitor.auto_start_in_tui,
                    id="monitor-auto-start",
                )
            yield Static(
                "  Per-monitor paths, schedules, policies, retention, pins, "
                "and alerts are managed in Monitor Center (2).",
                classes="sysinfo-value",
            )

            yield Static("")
            yield Static("UI Settings", classes="section-title")
            with Horizontal(classes="setting-row"):
                yield Label("Default visualization", classes="setting-label")
                yield Select(
                    [
                        ("Treemap", "treemap"),
                        ("Sunburst", "sunburst"),
                        ("Details", "details"),
                    ],
                    value=self._config.ui.default_viz,
                    id="default-viz",
                    classes="viz-select",
                    allow_blank=False,
                )

            with Horizontal(classes="setting-row"):
                yield Label("Color theme", classes="setting-label")
                yield Select(
                    [
                        ("Default", "default"),
                        ("Cold", "cold"),
                        ("Warm", "warm"),
                        ("Vivid", "vivid"),
                        ("Mono", "mono"),
                    ],
                    value=self._config.ui.color_theme,
                    id="color-theme",
                    classes="viz-select",
                    allow_blank=False,
                )

            with Horizontal(classes="setting-row"):
                yield Label("Hostname-aware paths", classes="setting-label")
                yield Switch(
                    value=self._config.ui.hostname_aware_paths,
                    id="hostname-aware-paths",
                )
                yield Label(
                    f"(host: {current_hostname()})",
                    classes="input-hint",
                )

            with Horizontal(classes="setting-row"):
                yield Label("Safe rendering (web shells)", classes="setting-label")
                yield Switch(
                    value=self._config.ui.safe_rendering,
                    id="safe-rendering",
                )
                yield Label(
                    "(ASCII bars and glyphs)",
                    classes="input-hint",
                )

            with Horizontal(classes="setting-row"):
                yield Label("Live scan rendering", classes="setting-label")
                yield Select(
                    [
                        ("Auto", "auto"),
                        ("On", "on"),
                        ("Off", "off"),
                    ],
                    value=self._config.ui.live_scan_render,
                    id="live-scan-render",
                    classes="viz-select",
                    allow_blank=False,
                )
                yield Label(
                    "(redraw viz during scan)",
                    classes="input-hint",
                )

        yield Footer()

    def on_mount(self) -> None:
        # Pre-fill inputs from config
        if self._config.scan.workers is not None:
            self.query_one("#workers-input", Input).value = str(self._config.scan.workers)
        if self._config.scan.max_depth is not None:
            self.query_one("#max-depth-input", Input).value = str(self._config.scan.max_depth)
        if self._config.monitor.max_watch_time is not None:
            self.query_one("#max-watch-time-input", Input).value = format_duration(self._config.monitor.max_watch_time)

        # Detect system info
        self._detect_system()
        self._detect_db_size()

    def on_screen_resume(self) -> None:
        self.query_one("#monitor-auto-start", Switch).value = (
            self._config.monitor.auto_start_in_tui
        )

    def _detect_system(self) -> None:
        """Detect system info and update display."""
        from sizetrail.scanner.sysinfo import detect_system_info

        info = detect_system_info(self._scan_path)
        self._system_info = info

        self.query_one("#sysinfo-cpus", Static).update(
            f"  CPUs: {info.available_cpus} available / {info.cpu_count} total"
        )

        if info.memory_total_mb > 0:
            total_str = self._format_memory(info.memory_total_mb)
            avail_str = self._format_memory(info.memory_available_mb)
            self.query_one("#sysinfo-memory", Static).update(
                f"  Memory: {avail_str} available / {total_str} total"
            )
        else:
            self.query_one("#sysinfo-memory", Static).update(
                "  Memory: unavailable"
            )

        load = info.load_average
        self.query_one("#sysinfo-load", Static).update(
            f"  Load: {load[0]:.2f} (1min) / {load[1]:.2f} (5min) / {load[2]:.2f} (15min)"
        )

        # Use the shared classifier so this line agrees with the FS-Overview
        # screen — it previously ignored network mounts and RAM-backed FSes.
        label, _ = storage_class(
            info.fs_type, info.is_network_fs, info.is_rotational
        )
        self.query_one("#sysinfo-storage", Static).update(
            f"  Storage: {info.fs_type} — {label}"
        )

        self.query_one("#sysinfo-recommendation", Static).update(
            f"  Recommended workers: {info.recommendation_reason}"
        )

        # Update hint next to workers input
        self.query_one("#workers-hint", Label).update(
            f"(recommended: {info.recommended_workers})"
        )

    def _detect_db_size(self) -> None:
        """Show database file size."""
        import os
        import humanize

        if self._repository is None:
            return
        try:
            size = os.path.getsize(self._repository.path)
            self.query_one("#sysinfo-dbsize", Static).update(
                "  Database: "
                f"{humanize.naturalsize(size, binary=True)} "
                f"({self._repository.path})"
            )
        except OSError:
            self.query_one("#sysinfo-dbsize", Static).update(
                "  Database: not found"
            )

    @staticmethod
    def _format_memory(mb: int) -> str:
        """Format memory as GB when >= 1024 MB, otherwise MB."""
        if mb >= 1024:
            gb = mb / 1024
            return f"{gb:,.1f} GB"
        return f"{mb:,} MB"

    def action_dismiss_settings(self) -> None:
        """Save config and go back."""
        try:
            save_config(self._config)
        except OSError:
            # Most likely a full disk — tell the user their settings
            # didn't persist rather than failing silently.
            self.app.notify(
                "Couldn't save settings — disk may be full. Changes apply "
                "for this session only.",
                title="Settings not saved",
                severity="warning",
                timeout=8,
            )
        self.app.pop_screen()

    def action_focus_next_field(self) -> None:
        """Move focus to the next form field (arrow-down)."""
        self.focus_next()

    def action_focus_previous_field(self) -> None:
        """Move focus to the previous form field (arrow-up)."""
        self.focus_previous()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Disable the focus-cycling bindings while any Select dropdown is
        open, so its own arrow-key handling (cycle options) gets the
        keystroke instead. Returning False from check_action removes the
        binding for this keypress; just returning early from the action
        handler is not enough because the priority binding still
        *consumes* the key, so it never reaches the SelectOverlay.

        We can't read `self.focused` to detect the open Select: opening
        the dropdown moves focus to a SelectOverlay child, so the
        Select widget itself is no longer focused. Querying all Selects
        is robust to that.
        """
        if action in ("focus_next_field", "focus_previous_field"):
            if self._any_select_expanded():
                return False
        return True

    def _any_select_expanded(self) -> bool:
        """True if any Select on this screen currently has its dropdown
        open."""
        try:
            for select in self.query(Select):
                if getattr(select, "expanded", False):
                    return True
        except Exception:
            pass
        return False

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "show-cleanup":
            self._config.ui.show_cleanup = event.value
        elif event.switch.id and event.switch.id.startswith("cleanup-pack-"):
            pack = event.switch.id.removeprefix("cleanup-pack-")
            disabled = set(self._config.cleanup.disabled_rule_packs)
            if event.value:
                disabled.discard(pack)
            else:
                disabled.add(pack)
            self._config.cleanup.disabled_rule_packs = sorted(disabled)
        elif event.switch.id == "one-file-system":
            self._config.scan.one_file_system = event.value
        elif event.switch.id == "exclude-pseudo-filesystems":
            self._config.scan.exclude_pseudo_filesystems = event.value
        elif event.switch.id == "monitor-auto-start":
            self._config.monitor.auto_start_in_tui = event.value
        elif event.switch.id == "hostname-aware-paths":
            self._config.ui.hostname_aware_paths = event.value
        elif event.switch.id == "safe-rendering":
            self._config.ui.safe_rendering = event.value
            # Apply immediately so any new renders pick up the choice
            # without requiring a restart.
            from sizetrail.rendering import set_safe_rendering
            set_safe_rendering(event.value)
            self.app.refresh()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "monitor-event-mode":
            value = str(event.value)
            if value not in {"auto", "events", "periodic"}:
                return
            previous = self._config.monitor.event_mode
            try:
                service = getattr(self.app, "_monitor_service", None)
                if service is not None:
                    service.set_event_mode(value)
                self._config.monitor.event_mode = value
            except Exception as exc:
                self._config.monitor.event_mode = previous
                event.select.value = previous
                self.app.notify(
                    str(exc),
                    title="Filesystem events unavailable",
                    severity="error",
                )
        elif event.select.id == "default-viz":
            self._config.ui.default_viz = str(event.value)
        elif event.select.id == "color-theme":
            self._config.ui.color_theme = str(event.value)
            set_color_scheme(str(event.value))
        elif event.select.id == "live-scan-render":
            value = str(event.value)
            if value in ("auto", "on", "off"):
                self._config.ui.live_scan_render = value

    def on_input_changed(self, event: Input.Changed) -> None:
        value = event.value.strip()
        if event.input.id == "workers-input":
            if value == "":
                self._config.scan.workers = None
            else:
                try:
                    parsed = int(value)
                    if parsed > 0:
                        self._config.scan.workers = parsed
                except ValueError:
                    pass  # Silently ignore non-numeric input
        elif event.input.id == "max-depth-input":
            if value == "":
                self._config.scan.max_depth = None
            else:
                try:
                    parsed = int(value)
                    if parsed >= 0:
                        self._config.scan.max_depth = parsed
                except ValueError:
                    pass
        elif event.input.id == "interval-input":
            try:
                parsed = parse_duration(value)
                if parsed > 0:
                    self._config.monitor.default_interval = parsed
            except (ValueError, IndexError):
                pass
        elif event.input.id == "max-watch-time-input":
            if value == "":
                self._config.monitor.max_watch_time = None
            else:
                try:
                    parsed = parse_duration(value)
                    if parsed > 0:
                        self._config.monitor.max_watch_time = parsed
                except (ValueError, IndexError):
                    pass
        elif event.input.id == "monitor-soft-budget":
            if value == "":
                self._config.monitor.database_soft_budget = None
            else:
                try:
                    self._config.monitor.database_soft_budget = parse_size(value)
                except ValueError:
                    pass
        elif event.input.id == "monitor-hard-budget":
            if value == "":
                self._config.monitor.database_hard_budget = None
            else:
                try:
                    self._config.monitor.database_hard_budget = parse_size(value)
                except ValueError:
                    pass
