"""Configuration screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll, Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, Switch, Label, Input, Select

from fs_monitor.config import AppConfig, save_config, parse_duration, format_duration, current_hostname
from fs_monitor.storage.database import Database
from fs_monitor.viz.colors import SCHEMES, set_color_scheme


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

    def __init__(self, config: AppConfig, db: Database | None = None,
                 scan_path: str = "/", **kwargs):
        super().__init__(**kwargs)
        self._config = config
        self._db = db
        self._scan_path = scan_path
        self._system_info = None

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

            yield Static("")
            yield Static("Scan Settings", classes="section-title")
            with Horizontal(classes="setting-row"):
                yield Label("Show hidden files", classes="setting-label")
                yield Switch(value=self._config.ui.show_hidden, id="show-hidden")

            with Horizontal(classes="setting-row"):
                yield Label("Follow symlinks", classes="setting-label")
                yield Switch(value=self._config.scan.follow_symlinks, id="follow-symlinks")

            yield Static("")
            yield Static("Cleanup Settings", classes="section-title")
            with Horizontal(classes="setting-row"):
                yield Label("Show Cleanup mode (experimental)", classes="setting-label")
                yield Switch(value=self._config.ui.show_cleanup, id="show-cleanup")
            yield Static(
                "  Cleanup mode deletes files. Enable only when needed.",
                classes="sysinfo-value",
            )
            with Horizontal(classes="setting-row"):
                yield Label("Confirm dangerous deletions", classes="setting-label")
                yield Switch(
                    value=self._config.cleanup.require_confirm_dangerous,
                    id="confirm-dangerous",
                )

            yield Static("")
            yield Static("Monitor Settings", classes="section-title")
            with Horizontal(classes="setting-row"):
                yield Label("Snapshot retention (days)", classes="setting-label")
                yield Input(
                    value=str(self._config.monitor.snapshot_retention),
                    id="retention-input",
                    classes="setting-input",
                )
                yield Label("(default: 30)", classes="input-hint")

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
                yield Label("Strict path matching", classes="setting-label")
                yield Switch(
                    value=self._config.monitor.strict_path,
                    id="strict-path",
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

    def _detect_system(self) -> None:
        """Detect system info and update display."""
        from fs_monitor.scanner.sysinfo import detect_system_info

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

        storage_desc = info.fs_type
        if info.is_rotational is True:
            storage_desc += " (HDD)"
        elif info.is_rotational is False:
            storage_desc += " (SSD)"
        self.query_one("#sysinfo-storage", Static).update(
            f"  Storage: {storage_desc}"
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

        if self._db is None:
            return
        try:
            size = os.path.getsize(self._db.path)
            self.query_one("#sysinfo-dbsize", Static).update(
                f"  Database: {humanize.naturalsize(size, binary=True)} ({self._db.path})"
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
            pass  # Best-effort save
        self.app.pop_screen()

    def action_focus_next_field(self) -> None:
        """Move focus to the next form field (arrow-down).

        If a Select is currently open, defer to its own arrow handling
        instead of stealing the keystroke.
        """
        if self._select_is_expanded():
            return
        self.focus_next()

    def action_focus_previous_field(self) -> None:
        """Move focus to the previous form field (arrow-up)."""
        if self._select_is_expanded():
            return
        self.focus_previous()

    def _select_is_expanded(self) -> bool:
        focused = self.focused
        if focused is None:
            return False
        # Textual's Select uses `expanded` to indicate an open dropdown.
        return bool(getattr(focused, "expanded", False))

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "show-hidden":
            self._config.ui.show_hidden = event.value
        elif event.switch.id == "follow-symlinks":
            self._config.scan.follow_symlinks = event.value
        elif event.switch.id == "show-cleanup":
            self._config.ui.show_cleanup = event.value
        elif event.switch.id == "confirm-dangerous":
            self._config.cleanup.require_confirm_dangerous = event.value
        elif event.switch.id == "strict-path":
            self._config.monitor.strict_path = event.value
        elif event.switch.id == "hostname-aware-paths":
            self._config.ui.hostname_aware_paths = event.value
        elif event.switch.id == "safe-rendering":
            self._config.ui.safe_rendering = event.value
            # Apply immediately so any new renders pick up the choice
            # without requiring a restart.
            from fs_monitor.rendering import set_safe_rendering
            set_safe_rendering(event.value)
            self.app.refresh()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "default-viz":
            self._config.ui.default_viz = str(event.value)
        elif event.select.id == "color-theme":
            self._config.ui.color_theme = str(event.value)
            set_color_scheme(str(event.value))

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
        elif event.input.id == "retention-input":
            try:
                parsed = int(value)
                if parsed > 0:
                    self._config.monitor.snapshot_retention = parsed
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
