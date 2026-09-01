"""Main Textual App with mode/screen management."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from textual import events
from textual.app import App
from textual.binding import Binding

from disktide import APP_NAME
from disktide.config import (
    AppConfig, cleanup_rule_directory, load_config, save_config,
    get_effective_paths, set_effective_paths,
)
from disktide.rendering import (
    bump_render_epoch,
    set_ring_shape,
    set_safe_rendering,
)
from disktide.repositories import default_snapshot_repository
from disktide.repositories.snapshots import SnapshotRepository
from disktide.themes import resolve_theme
from disktide.viz import cellgeom
from disktide.viz.chrome import CHROME_THEMES
from disktide.viz.colors import SCHEMES, set_color_scheme
from disktide.widgets.confirm_modal import ConfirmModal
from disktide.widgets.monitor_editor import MonitorEditor, MonitorEditorResult

# The services and the mode screens are imported where they are first
# used, not here. Drawing the welcome screen needs none of them, and on a
# network filesystem the modules they pull in are the whole startup cost:
# a module file costs ~16ms to fault in from a cold NFS mount against
# ~0.4ms once the page cache holds it.
if TYPE_CHECKING:
    from disktide.domain.cleanup import CleanupActionKind
    from disktide.domain.monitor import MonitorDefinition
    from disktide.services.cleanup import CleanupService
    from disktide.services.monitor import MonitorService
    from disktide.services.scan import ScanService
    from disktide.services.visualization import VisualizationService


class DiskTideApp(App):
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
        ansi_only = "NO_COLOR" in os.environ
        if ansi_only:
            kwargs["ansi_color"] = True
        super().__init__(**kwargs)
        self._ansi_only = ansi_only
        # Registered here, before anything can be mounted: `App.theme` is a
        # validated reactive and assigning a name Textual has not seen
        # raises rather than falling back.
        for theme in CHROME_THEMES:
            self.register_theme(theme)
        # Textual's animations cost ~320ms of the ~420ms a viz tab switch
        # took, and each one is a burst of frames down an ssh pipe — this
        # tool's home is a login/compute node, not a local terminal.
        # TEXTUAL_ANIMATIONS is the user's own knob, so any value set there
        # keeps whatever Textual resolved from it.
        if not os.environ.get("TEXTUAL_ANIMATIONS"):
            self.animation_level = "none"
        self._scan_path = str(Path(scan_path).resolve()) if scan_path else None
        self._config = config or load_config()
        self._snapshot_repository = (
            snapshot_repository or default_snapshot_repository()
        )
        # Backing fields for the lazy service properties below.
        self.__scan_service: ScanService | None = None
        self.__monitor_service: MonitorService | None = None
        self.__visualization_service: VisualizationService | None = None
        self.__cleanup_service: CleanupService | None = None
        self.__cleanup_safe_action: CleanupActionKind | None = None
        self._show_welcome = show_welcome
        # Ensures the "running without persistence" warning is only shown
        # once per session, no matter how many times we check.
        self._warned_degraded = False

    # Services build themselves on first touch. The attribute names are
    # unchanged, so screens and tests still reach them as `_scan_service`
    # and friends without knowing they were deferred.

    @property
    def _scan_service(self) -> ScanService:
        if self.__scan_service is None:
            from disktide.services.scan import ScanService

            self.__scan_service = ScanService()
        return self.__scan_service

    @property
    def _monitor_service(self) -> MonitorService:
        if self.__monitor_service is None:
            from disktide.services.monitor import MonitorService

            self.__monitor_service = MonitorService(
                self._snapshot_repository,
                scan_service=self._scan_service,
                host_type="tui",
                soft_budget_bytes=self._config.monitor.database_soft_budget,
                hard_budget_bytes=self._config.monitor.database_hard_budget,
                event_mode=self._config.monitor.event_mode,
            )
        return self.__monitor_service

    @property
    def _visualization_service(self) -> VisualizationService:
        if self.__visualization_service is None:
            from disktide.services.visualization import VisualizationService

            self.__visualization_service = VisualizationService(
                self._snapshot_repository
            )
        return self.__visualization_service

    @property
    def _cleanup_service(self) -> CleanupService:
        if self.__cleanup_service is None:
            from disktide.cleanup.actions import QuarantineExecutor
            from disktide.cleanup.rules import get_rule_by_name
            from disktide.services.cleanup import CleanupService

            self.__cleanup_service = CleanupService(
                self._snapshot_repository,
                quarantine=QuarantineExecutor(
                    retention_days=(
                        self._config.cleanup.quarantine_retention_days
                    ),
                    max_bytes=self._config.cleanup.quarantine_max_bytes,
                ),
                rule_provider=lambda name: get_rule_by_name(
                    name,
                    disabled_packs=self._config.cleanup.disabled_rule_packs,
                    user_directory=cleanup_rule_directory(),
                ),
            )
        return self.__cleanup_service

    @property
    def _cleanup_safe_action(self) -> CleanupActionKind:
        if self.__cleanup_safe_action is None:
            from disktide.domain.cleanup import CleanupActionKind

            self.__cleanup_safe_action = (
                CleanupActionKind.TRASH
                if self._config.cleanup.prefer_trash
                else CleanupActionKind.QUARANTINE
            )
        return self.__cleanup_safe_action

    def apply_color_theme(self, name: str) -> str:
        """Apply a colour theme to both halves of the UI, and say which landed.

        The charts read `viz.colors` and the chrome reads Textual's theme
        registry, and nothing connects the two but this call — so the
        settings picker and startup go through here rather than each
        remembering to do both. Returns the resolved key so a caller with a
        legacy or hand-typed name can write back what was actually used.

        Under `NO_COLOR` the Textual theme is left alone: the app is already
        in ansi mode, where every hex the theme carries is discarded, and
        switching it would only churn the CSS.
        """
        resolved = resolve_theme(name)
        set_color_scheme(resolved)
        if not self._ansi_only:
            self.theme = SCHEMES[resolved].textual_theme
        return resolved

    def on_mount(self) -> None:
        self._config.ui.color_theme = self.apply_color_theme(
            self._config.ui.color_theme
        )
        set_safe_rendering(self._config.ui.safe_rendering)
        # The environment wins over the config so a shape can be asked for
        # per-launch — which is the whole of how the two are compared,
        # including by the README capture pipeline.
        self._config.ui.ring_shape = set_ring_shape(
            os.environ.get("DISKTIDE_RING_SHAPE") or self._config.ui.ring_shape
        )
        cellgeom.set_configured_aspect(self._config.ui.cell_aspect)

        # Connect the DB up front so a fallback to an in-memory database is
        # detected before the session starts. The warning is
        # emitted after the first screen is pushed (below), so the toast
        # lands on a visible screen rather than the pre-mount default one.
        self._snapshot_repository.connect()

        if self._show_welcome:
            from disktide.screens.welcome import WelcomeScreen

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

    def on_resize(self, event: events.Resize) -> None:
        """Take the terminal's pixel size from an in-band resize report.

        Terminals that support mode 2048 send their pixel size with every
        resize, which is the only measurement available in a good few of
        them: VS Code's integrated terminal and the xterm.js web shells
        leave `TIOCGWINSZ`'s pixel fields at zero forever but will happily
        report in band. SIGWINCH-driven resizes carry `pixel_size=None`,
        so this fires only when there is something real to learn.

        Textual dispatches `_on_resize` and `on_resize` both, so the
        built-in one still runs; this only adds to it. The charts are not
        touched directly — a widget mid-paint must never have its layout
        swapped underneath it (see `SunburstView._ensure_layout`) — so a
        changed aspect goes out as a render-epoch bump and each chart
        rebuilds on its own deferred path.
        """
        pixel_size = event.pixel_size
        if pixel_size is None or pixel_size.width <= 0 or pixel_size.height <= 0:
            return
        before = cellgeom.detect_cell_aspect()
        if not cellgeom.report_pixel_size(event.size, pixel_size):
            return
        if abs(cellgeom.detect_cell_aspect() - before) > cellgeom.ASPECT_EPSILON:
            bump_render_epoch()

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
        # Deferred to here rather than module scope: between them these
        # five screens reach about a third of the app's import graph (the
        # monitor screen alone reaches plotext through the trend chart),
        # and none of it is needed until the user has picked a path.
        from disktide.screens.cleanup import CleanupScreen
        from disktide.screens.explorer import ExplorerScreen
        from disktide.screens.fs_overview import FSOverviewScreen
        from disktide.screens.monitor import MonitorScreen
        from disktide.screens.settings import SettingsScreen

        self._scan_path = scan_path

        self._explorer = ExplorerScreen(
            self._scan_path,
            config=self._config,
            scan_service=self._scan_service,
            visualization_service=self._visualization_service,
            monitor_service=self._monitor_service,
        )
        self._cleanup = CleanupScreen(
            service=self._cleanup_service,
            safe_action=self._cleanup_safe_action,
            config=self._config,
        )
        self._monitor = MonitorScreen(
            service=self._monitor_service,
            config=self._config,
            visualization_service=self._visualization_service,
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
        boundary, so the worker thread bails out quickly. Once the TUI
        has torn down, the terminal side cancels the scan, closes the
        database, and exits without printing anything — see
        disktide.__main__.
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
        # Reached through the backing fields, not the properties: quitting
        # from the welcome screen must not build a service — and pay for
        # its imports — purely to shut it down again.
        if self.__monitor_service is not None:
            try:
                self.__monitor_service.stop_session(wait=False)
            except Exception:
                pass
        try:
            self._explorer.cancel_active_scan()
        except AttributeError:
            if self.__scan_service is not None:
                self.__scan_service.cancel_all()
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
            selected_path = self._explorer.selected_path
            self._monitor.set_navigation_context(root_path, selected_path)
            self.switch_screen("monitor")
        elif mode == "fs_overview":
            self.switch_screen("fs_overview")
