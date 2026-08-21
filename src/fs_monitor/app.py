"""Main Textual App with mode/screen management."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from textual.app import App
from textual.binding import Binding

from fs_monitor import APP_NAME
from fs_monitor.config import (
    AppConfig, load_config, save_config,
    get_effective_paths, set_effective_paths,
)
from fs_monitor.domain.monitor import MonitorDefinition
from fs_monitor.rendering import set_safe_rendering
from fs_monitor.repositories import default_snapshot_repository
from fs_monitor.repositories.snapshots import SnapshotRepository
from fs_monitor.services.scan import ScanService
from fs_monitor.services.monitor import MonitorService
from fs_monitor.viz.colors import set_color_scheme
from fs_monitor.screens.explorer import ExplorerScreen
from fs_monitor.screens.cleanup import CleanupScreen
from fs_monitor.screens.monitor import MonitorScreen
from fs_monitor.screens.settings import SettingsScreen
from fs_monitor.screens.fs_overview import FSOverviewScreen
from fs_monitor.widgets.confirm_modal import ConfirmModal
from fs_monitor.widgets.monitor_editor import MonitorEditor, MonitorEditorResult


class FSMonitorApp(App):
    """Interactive terminal disk usage explorer."""

    TITLE = APP_NAME
    SUB_TITLE = "Disk Usage Explorer"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding(
            "1",
            "switch_mode('explorer')",
            "[1]Explorer [2]Monitor [3]FS-Overview",
            show=True,
            key_display="Mode",
        ),
        Binding("2", "switch_mode('monitor')", "Monitor", show=False),
        Binding("3", "switch_mode('fs_overview')", "FS Overview", show=False),
        Binding("c", "switch_mode('cleanup')", "Cleanup", show=False),
        Binding("question_mark", "push_screen('settings')", "Settings", show=True, key_display="?"),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(
        self,
        scan_path: str | None = None,
        config: AppConfig | None = None,
        show_welcome: bool = False,
        snapshot_repository: SnapshotRepository | None = None,
        **kwargs,
    ):
        if "NO_COLOR" in os.environ:
            kwargs["ansi_color"] = True
        super().__init__(**kwargs)
        self._scan_path = str(Path(scan_path).resolve()) if scan_path else None
        self._config = config or load_config()
        self._snapshot_repository = (
            snapshot_repository or default_snapshot_repository()
        )
        self._scan_service = ScanService()
        self._monitor_service = MonitorService(
            self._snapshot_repository,
            scan_service=self._scan_service,
            host_type="tui",
            soft_budget_bytes=self._config.monitor.database_soft_budget,
            hard_budget_bytes=self._config.monitor.database_hard_budget,
        )
        self._show_welcome = show_welcome
        # Ensures the "running without persistence" warning is only shown
        # once per session, no matter how many times we check.
        self._warned_degraded = False

    def on_mount(self) -> None:
        set_color_scheme(self._config.ui.color_theme)
        set_safe_rendering(self._config.ui.safe_rendering)

        # Connect the DB up front so a fallback to an in-memory database is
        # detected before the session starts. The warning is
        # emitted after the first screen is pushed (below), so the toast
        # lands on a visible screen rather than the pre-mount default one.
        self._snapshot_repository.connect()

        if self._show_welcome:
            from fs_monitor.screens.welcome import WelcomeScreen

            paths = get_effective_paths(self._config)
            cwd = os.getcwd()
            recent = self._snapshot_repository.recent_paths(limit=5)

            self.push_screen(
                WelcomeScreen(
                    cwd_path=cwd,
                    saved_path=paths.default_scan_path,
                    last_visited_path=paths.last_visited_path,
                    recent_paths=recent,
                ),
                callback=self._on_welcome_result,
            )
        else:
            self._launch_explorer(self._scan_path or str(Path(".").resolve()))

        if (
            self._config.monitor.auto_start_in_tui
            and self._snapshot_repository.status.writable
        ):
            try:
                self._monitor_service.start_session(host_type="tui")
            except Exception:
                pass

        self._warn_if_degraded()

    def _warn_if_degraded(self) -> None:
        """Warn once if the database fell back to memory.

        Surfaced on every launch path so the user always learns that
        snapshots/history won't be persisted this session, regardless of
        whether they came in through the welcome screen or straight into
        the explorer.
        """
        status = self._snapshot_repository.status
        if status.writable or self._warned_degraded:
            return
        self._warned_degraded = True
        if status.read_only:
            message = (
                "The snapshot database is read-only. Existing history can "
                "be viewed, but new snapshots will not be saved."
            )
        else:
            message = (
                "The storage database is unavailable. Snapshots and history "
                "won't be saved this session; file exploration still works."
            )
        self.notify(
            message,
            title="Running without persistence",
            severity="warning",
            timeout=10,
        )

    def _on_welcome_result(self, result: tuple[str, bool] | None) -> None:
        """Callback from WelcomeScreen with the chosen path and save flag."""
        if result is None or self._exit:
            return
        path, save_default = result
        paths = get_effective_paths(self._config)
        changed = False
        if save_default and paths.default_scan_path != path:
            paths.default_scan_path = path
            changed = True
        if paths.last_visited_path != path:
            paths.last_visited_path = path
            changed = True
        if changed:
            set_effective_paths(self._config, paths)
            try:
                save_config(self._config)
            except OSError:
                pass
        self._launch_explorer(path)

    def _save_last_visited(self, path: str) -> None:
        """Persist the last-visited path to config."""
        paths = get_effective_paths(self._config)
        if paths.last_visited_path == path:
            return
        paths.last_visited_path = path
        set_effective_paths(self._config, paths)
        try:
            save_config(self._config)
        except OSError:
            pass

    def _launch_explorer(self, scan_path: str) -> None:
        """Install mode screens and push the explorer."""
        self._scan_path = scan_path

        self._explorer = ExplorerScreen(
            self._scan_path,
            config=self._config,
            scan_service=self._scan_service,
        )
        self._cleanup = CleanupScreen()
        self._monitor = MonitorScreen(
            service=self._monitor_service,
            config=self._config,
            root_path=self._scan_path,
            selected_path=self._scan_path,
        )
        self._fs_overview = FSOverviewScreen()

        self.install_screen(self._explorer, name="explorer")
        self.install_screen(self._cleanup, name="cleanup")
        self.install_screen(self._monitor, name="monitor")
        self.install_screen(self._fs_overview, name="fs_overview")
        self.install_screen(
            SettingsScreen(
                self._config,
                repository=self._snapshot_repository,
                scan_path=self._scan_path,
            ),
            name="settings",
        )

        self.push_screen("explorer")

    def open_monitor_setup(
        self,
        default_path: str,
        *,
        on_created: Callable[[MonitorDefinition], None] | None = None,
    ) -> None:
        """Open the shared monitor editor and persist its result."""

        def _on_result(result: MonitorEditorResult | None) -> None:
            if result is None:
                return
            created: MonitorDefinition | None = None
            try:
                warnings = self._monitor_service.definition_warnings(
                    result.definition
                )
                created = self._monitor_service.create_monitor(result.definition)
                for warning in warnings:
                    self.notify(warning, severity="warning", timeout=6)
                if result.capture_now and created.id is not None:
                    if not self._monitor_service.session_running:
                        self._monitor_service.start_session(host_type="tui")
                    self._monitor_service.run_monitor_now(created.id)
            except Exception as exc:
                self.notify(
                    f"{type(exc).__name__}: {exc}",
                    title="Monitor setup failed",
                    severity="error",
                    timeout=8,
                )
            if created is not None and on_created is not None:
                on_created(created)

        self.push_screen(
            MonitorEditor(config=self._config, default_path=default_path),
            callback=_on_result,
        )

    def action_quit(self) -> None:
        """Gate quit behind a y/n prompt to avoid accidental exits.

        On confirm, the real teardown runs in `_perform_quit`. The
        scanner checks the service cancellation token at every directory
        boundary, so the worker thread bails out quickly. The terminal
        side prints "Exiting..." after TUI teardown and joins the
        scanner thread before printing the final goodbye — see
        fs_monitor.__main__.
        """
        # If a modal (e.g. the quit prompt itself) is already on top,
        # ignore repeated `q` presses so we don't stack prompts.
        if isinstance(self.screen, ConfirmModal):
            return

        def _on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self._perform_quit()

        self.push_screen(
            ConfirmModal(
                message=f"Quit {APP_NAME}?",
                title="Quit",
                confirm_keys=("q",),
            ),
            callback=_on_confirm,
        )

    def _perform_quit(self) -> None:
        """Save config, cancel active scans, then exit the app."""
        try:
            save_config(self._config)
        except OSError:
            pass
        try:
            self._monitor_service.stop_session(wait=False)
        except Exception:
            pass
        try:
            self._explorer.cancel_active_scan()
        except AttributeError:
            self._scan_service.cancel_all()
        self.exit()

    def action_switch_mode(self, mode: str) -> None:
        """Switch between explorer/cleanup/monitor/fs_overview modes."""
        if mode == "explorer":
            self.switch_screen("explorer")
        elif mode == "cleanup":
            if not self._config.ui.show_cleanup:
                self.notify(
                    "Cleanup mode is disabled. Enable it in Settings (?).",
                    severity="warning",
                )
                return
            # Pass current root from explorer to cleanup
            if self._explorer._root is not None:
                self._cleanup.set_root(self._explorer._root)
            self.switch_screen("cleanup")
        elif mode == "monitor":
            root_path = (
                self._explorer._root.path
                if self._explorer._root is not None
                else self._explorer._scan_path
            )
            selected_path = (
                self._explorer._current.path
                if self._explorer._current is not None
                else root_path
            )
            self._monitor.set_navigation_context(root_path, selected_path)
            self.switch_screen("monitor")
        elif mode == "fs_overview":
            self.switch_screen("fs_overview")
