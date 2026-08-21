"""Wave 06 Monitor Center setup, parity, navigation, and session UX."""

from __future__ import annotations

import asyncio
import inspect

from fs_monitor.app import FSMonitorApp
from fs_monitor.config import AppConfig
from fs_monitor.domain.monitor import MonitorActivityState, MonitorDefinition
from fs_monitor.repositories.sqlite import SQLiteSnapshotRepository
from fs_monitor.screens.explorer import ExplorerScreen
from fs_monitor.screens.monitor import MonitorScreen
from fs_monitor.widgets.alert_editor import AlertEditor
from fs_monitor.widgets.monitor_editor import MonitorEditor


async def _wait_for_explorer(pilot, app) -> None:
    for _ in range(80):
        await pilot.pause(0.025)
        if getattr(app.screen, "_scan_in_progress", True) is False:
            return


async def _wait_for_monitor_load(pilot, screen: MonitorScreen) -> None:
    for _ in range(80):
        await pilot.pause(0.025)
        if not screen._loading:
            return


def _config() -> AppConfig:
    config = AppConfig()
    config.scan.workers = 1
    config.ui.live_scan_render = "off"
    return config


def test_tui_setup_is_visible_to_shared_service_and_alert_editor(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_text("x")
    repository = SQLiteSnapshotRepository(path=str(tmp_path / "tui.db"))

    async def exercise() -> None:
        app = FSMonitorApp(
            scan_path=str(root),
            show_welcome=False,
            config=_config(),
            snapshot_repository=repository,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("2")
            await pilot.pause(0.2)
            screen = app.screen
            assert isinstance(screen, MonitorScreen)
            await _wait_for_monitor_load(pilot, screen)
            assert "No monitor definition" in str(
                screen.query_one("#monitor-overview").render()
            )

            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, MonitorEditor)
            app.screen.query_one("#monitor-interval").value = "10m"
            await pilot.press("ctrl+s")
            await pilot.pause(0.3)
            screen = app.screen
            assert isinstance(screen, MonitorScreen)
            await _wait_for_monitor_load(pilot, screen)

            definitions = app._monitor_service.list_monitors()
            assert len(definitions) == 1
            assert definitions[0].root_path == str(root)
            assert definitions[0].interval_seconds == 600
            assert screen.query_one("#monitor-list").row_count == 1

            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, AlertEditor)
            app.screen.query_one("#alert-threshold").value = "1B"
            await pilot.press("ctrl+s")
            await pilot.pause(0.3)
            assert len(app._monitor_service.list_alert_rules(definitions[0].id)) == 1

            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, MonitorEditor)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, MonitorScreen)

        app._monitor_service.shutdown(wait=True)

    try:
        asyncio.run(exercise())
    finally:
        repository.close()


def test_narrow_monitor_uses_list_detail_and_session_continues_off_screen(
    tmp_path,
):
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_text("x")
    path = tmp_path / "narrow.db"
    bootstrap = SQLiteSnapshotRepository(path=str(path))
    bootstrap.connect()
    monitor = bootstrap.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=3600)
    )
    bootstrap.close()
    repository = SQLiteSnapshotRepository(path=str(path))

    async def exercise() -> None:
        app = FSMonitorApp(
            scan_path=str(root),
            show_welcome=False,
            config=_config(),
            snapshot_repository=repository,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("2")
            await pilot.pause(0.2)
            screen = app.screen
            assert isinstance(screen, MonitorScreen)
            await _wait_for_monitor_load(pilot, screen)
            assert screen.has_class("narrow")
            mode_binding = app.active_bindings["1"].binding
            assert mode_binding.key_display == "Mode"
            assert (
                mode_binding.description
                == "[1]Explorer [2]Monitor [3]FS-Overview"
            )
            footer = screen.query_one("Footer")
            assert any(
                getattr(item, "key_display", None) == "Mode"
                and getattr(item, "description", None)
                == "[1]Explorer [2]Monitor [3]FS-Overview"
                for item in footer.children
            )
            screen.query_one("#monitor-list").focus()
            await pilot.press("enter")
            await pilot.pause()
            assert screen.has_class("detail")
            await pilot.press("escape")
            assert not screen.has_class("detail")

            await pilot.press("s")
            await pilot.pause(0.2)
            assert app._monitor_service.session_running
            await pilot.press("1")
            await pilot.pause()
            assert isinstance(app.screen, ExplorerScreen)
            assert app._monitor_service.session_running
            app._monitor_service.stop_session(wait=True)
            status = repository.get_monitor_status(monitor.id)
            assert status.activity is MonitorActivityState.NO_HOST
            assert status.host_id is None

        app._monitor_service.shutdown(wait=True)

    try:
        asyncio.run(exercise())
    finally:
        repository.close()


def test_explorer_sets_up_monitor_for_highlighted_directory(tmp_path):
    root = tmp_path / "root"
    selected = root / "selected"
    selected.mkdir(parents=True)
    (selected / "payload").write_text("x")
    repository = SQLiteSnapshotRepository(path=str(tmp_path / "explorer.db"))

    async def exercise() -> None:
        app = FSMonitorApp(
            scan_path=str(root),
            show_welcome=False,
            config=_config(),
            snapshot_repository=repository,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree")
            await pilot.press("down")
            await pilot.pause()
            assert tree.cursor_node.data.path == str(selected)

            await pilot.press("shift+m")
            await pilot.pause()
            assert isinstance(app.screen, MonitorEditor)
            assert app.screen.query_one("#monitor-path").value == str(selected)

            await pilot.press("ctrl+s")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ExplorerScreen)
            definitions = app._monitor_service.list_monitors()
            assert len(definitions) == 1
            assert definitions[0].root_path == str(selected)

        app._monitor_service.shutdown(wait=True)

    try:
        asyncio.run(exercise())
    finally:
        repository.close()


def test_monitor_screen_depends_on_service_not_sqlite_adapter():
    source = inspect.getsource(MonitorScreen)
    assert "SQLite" not in source
    assert "Database(" not in source
    assert "default_snapshot_repository" not in source
