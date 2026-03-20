"""Main Textual App with mode/screen management."""

from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.binding import Binding

from fs_monitor.config import AppConfig, load_config
from fs_monitor.storage.database import Database
from fs_monitor.screens.explorer import ExplorerScreen
from fs_monitor.screens.cleanup import CleanupScreen
from fs_monitor.screens.monitor import MonitorScreen
from fs_monitor.screens.settings import SettingsScreen


class FSMonitorApp(App):
    """Interactive terminal disk usage explorer."""

    TITLE = "fsmonitor-cli"
    SUB_TITLE = "Disk Usage Explorer"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("e", "switch_mode('explorer')", "[E]xplorer [C]leanup [M]onitor", show=True, key_display="Mode"),
        Binding("c", "switch_mode('cleanup')", "Cleanup", show=False),
        Binding("m", "switch_mode('monitor')", "Monitor", show=False),
        Binding("question_mark", "push_screen('settings')", "Settings", show=True, key_display="?"),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, scan_path: str, config: AppConfig | None = None, **kwargs):
        super().__init__(**kwargs)
        self._scan_path = str(Path(scan_path).resolve())
        self._config = config or load_config()
        self._db = Database()

    def on_mount(self) -> None:
        # Override MODES to pass arguments
        # We need to install screens manually since MODES requires no-arg constructors
        self._explorer = ExplorerScreen(self._scan_path, config=self._config)
        self._cleanup = CleanupScreen()
        self._monitor = MonitorScreen(db=self._db, root_path=self._scan_path)

        self.install_screen(self._explorer, name="explorer")
        self.install_screen(self._cleanup, name="cleanup")
        self.install_screen(self._monitor, name="monitor")
        self.install_screen(
            SettingsScreen(self._config), name="settings"
        )

        self.push_screen("explorer")

    def action_switch_mode(self, mode: str) -> None:
        """Switch between explorer/cleanup/monitor modes."""
        if mode == "explorer":
            self.switch_screen("explorer")
        elif mode == "cleanup":
            # Pass current root from explorer to cleanup
            if self._explorer._root is not None:
                self._cleanup.set_root(self._explorer._root)
            self.switch_screen("cleanup")
        elif mode == "monitor":
            self.switch_screen("monitor")
