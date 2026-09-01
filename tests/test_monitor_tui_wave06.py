"""Wave 06 Monitor Center setup, parity, navigation, and session UX."""

from __future__ import annotations

import asyncio
import inspect

from textual.widgets import Button

from disktide.app import DiskTideApp
from disktide.config import AppConfig
from disktide.domain.monitor import MonitorActivityState, MonitorDefinition
from disktide.repositories.sqlite import SQLiteSnapshotRepository
from disktide.screens.explorer import ExplorerScreen
from disktide.screens.monitor import MonitorScreen
from disktide.widgets.alert_editor import AlertEditor
from disktide.widgets.monitor_editor import MonitorEditor
from tests.waiting import wait_for_explorer, wait_until


async def _wait_for_monitor_load(pilot, screen: MonitorScreen) -> None:
    await wait_until(
        pilot,
        lambda: not screen._loading,
        what="the Monitor Center never finished loading",
        tries=200,
        delay=0.025,
    )


async def _settle(pilot, predicate, tries: int = 160) -> None:
    """Pump the message loop until ``predicate`` holds, or say which one did not.

    Service flags flip on worker threads before the callbacks that rewrite the
    UI reach the Textual message loop, so a single fixed pause is not a
    synchronisation point on a busy (2-CPU CI) host. Most callers re-assert the
    same condition right after, which reports the real value rather than a bare
    timeout; the ones that go straight on to `query_one` do not, and a silent
    return left those raising `NoMatches` from inside the widget instead.
    """
    await wait_until(pilot, predicate, tries=tries, delay=0.05)


async def _drain_monitor_loads(pilot, app) -> None:
    """Let in-flight Monitor Center reloads finish before the app tears down.

    MonitorScreen._load_data runs on a thread worker; one still populating
    widgets while run_test() unmounts the screen fails with NoMatches, which
    Textual reports as a WorkerFailed crash rather than as a test assertion.
    """
    await _settle(
        pilot,
        lambda: not any(
            worker.group == "monitor-load" and worker.is_running
            for worker in app.workers
        ),
    )


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
        app = DiskTideApp(
            scan_path=str(root),
            show_welcome=False,
            config=_config(),
            snapshot_repository=repository,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("2")
            await _settle(pilot, lambda: isinstance(app.screen, MonitorScreen))
            screen = app.screen
            assert isinstance(screen, MonitorScreen)
            await _wait_for_monitor_load(pilot, screen)
            await _settle(
                pilot,
                lambda: "No monitor definition"
                in str(screen.query_one("#monitor-history-summary").render()),
            )
            assert screen.query_one("#monitor-tabs").active == "monitor-history-tab"
            assert screen.query_one("#monitor-session-toggle", Button).disabled
            assert "No monitor definition" in str(
                screen.query_one("#monitor-history-summary").render()
            )
            assert "No monitor definition" in str(
                screen.query_one("#monitor-overview").render()
            )

            await pilot.press("n")
            await _settle(pilot, lambda: isinstance(app.screen, MonitorEditor))
            assert isinstance(app.screen, MonitorEditor)
            app.screen.query_one("#monitor-interval").value = "10m"
            await pilot.press("ctrl+s")
            await _settle(pilot, lambda: isinstance(app.screen, MonitorScreen))
            screen = app.screen
            assert isinstance(screen, MonitorScreen)
            await _wait_for_monitor_load(pilot, screen)
            await _settle(
                pilot, lambda: screen.query_one("#monitor-list").row_count == 1
            )

            definitions = app._monitor_service.list_monitors()
            assert len(definitions) == 1
            assert definitions[0].root_path == str(root)
            assert definitions[0].interval_seconds == 600
            assert screen.query_one("#monitor-list").row_count == 1
            assert not screen.query_one("#monitor-session-toggle", Button).disabled

            await pilot.press("a")
            await _settle(pilot, lambda: isinstance(app.screen, AlertEditor))
            assert isinstance(app.screen, AlertEditor)
            app.screen.query_one("#alert-threshold").value = "1B"
            await pilot.press("ctrl+s")
            await _settle(
                pilot,
                lambda: len(
                    app._monitor_service.list_alert_rules(definitions[0].id)
                )
                == 1,
            )
            assert len(app._monitor_service.list_alert_rules(definitions[0].id)) == 1

            await pilot.press("e")
            await _settle(pilot, lambda: isinstance(app.screen, MonitorEditor))
            assert isinstance(app.screen, MonitorEditor)
            await pilot.press("escape")
            await _settle(pilot, lambda: isinstance(app.screen, MonitorScreen))
            assert isinstance(app.screen, MonitorScreen)

        app._monitor_service.shutdown(wait=True)

    try:
        asyncio.run(exercise())
    finally:
        repository.close()


def test_monitor_sampling_controls_start_stop_and_persist_auto_start(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_text("x")
    path = tmp_path / "controls.db"
    bootstrap = SQLiteSnapshotRepository(path=str(path))
    bootstrap.connect()
    monitor = bootstrap.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=3600)
    )
    bootstrap.close()
    repository = SQLiteSnapshotRepository(path=str(path))
    config = _config()
    saved_auto_start: list[bool] = []
    monkeypatch.setattr(
        "disktide.screens.monitor.save_config",
        lambda current: saved_auto_start.append(
            current.monitor.auto_start_in_tui
        ),
    )

    async def exercise() -> None:
        app = DiskTideApp(
            scan_path=str(root),
            show_welcome=False,
            config=config,
            snapshot_repository=repository,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("2")
            await _settle(pilot, lambda: isinstance(app.screen, MonitorScreen))
            screen = app.screen
            assert isinstance(screen, MonitorScreen)
            await _wait_for_monitor_load(pilot, screen)

            session_button = screen.query_one("#monitor-session-toggle", Button)
            auto_start_button = screen.query_one(
                "#monitor-auto-start-toggle", Button
            )
            await _settle(
                pilot, lambda: session_button.label.plain == "Start sampling [S]"
            )
            assert session_button.label.plain == "Start sampling [S]"
            assert auto_start_button.label.plain == "Auto-start: Off"

            auto_start_button.press()
            await _settle(
                pilot, lambda: auto_start_button.label.plain == "Auto-start: On"
            )
            assert config.monitor.auto_start_in_tui is True
            assert saved_auto_start == [True]
            assert auto_start_button.label.plain == "Auto-start: On"

            session_button.press()
            await _settle(pilot, lambda: app._monitor_service.session_running)
            assert app._monitor_service.session_running
            await _wait_for_monitor_load(pilot, screen)
            await _settle(
                pilot, lambda: session_button.label.plain == "Stop & cancel [S]"
            )
            assert session_button.label.plain == "Stop & cancel [S]"

            session_button.press()
            await _settle(pilot, lambda: not app._monitor_service.session_running)
            assert not app._monitor_service.session_running
            await _wait_for_monitor_load(pilot, screen)
            await _settle(
                pilot, lambda: session_button.label.plain == "Start sampling [S]"
            )
            assert session_button.label.plain == "Start sampling [S]"
            await _drain_monitor_loads(pilot, app)
            status = repository.get_monitor_status(monitor.id)
            assert status.activity is MonitorActivityState.NO_HOST
            assert status.host_id is None

        app._monitor_service.shutdown(wait=True)

    try:
        asyncio.run(exercise())
    finally:
        repository.close()


def test_monitor_auto_start_launch_is_reflected_in_controls(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_text("x")
    path = tmp_path / "auto-start.db"
    bootstrap = SQLiteSnapshotRepository(path=str(path))
    bootstrap.connect()
    bootstrap.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=3600)
    )
    bootstrap.close()
    repository = SQLiteSnapshotRepository(path=str(path))
    config = _config()
    config.monitor.auto_start_in_tui = True

    async def exercise() -> None:
        app = DiskTideApp(
            scan_path=str(root),
            show_welcome=False,
            config=config,
            snapshot_repository=repository,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            await _settle(pilot, lambda: app._monitor_service.session_running)
            assert app._monitor_service.session_running
            await pilot.press("2")
            await _settle(pilot, lambda: isinstance(app.screen, MonitorScreen))
            screen = app.screen
            assert isinstance(screen, MonitorScreen)
            await _wait_for_monitor_load(pilot, screen)
            await _settle(
                pilot,
                lambda: screen.query_one(
                    "#monitor-session-toggle", Button
                ).label.plain
                == "Stop & cancel [S]",
            )
            assert (
                screen.query_one("#monitor-session-toggle", Button).label.plain
                == "Stop & cancel [S]"
            )
            assert (
                screen.query_one("#monitor-auto-start-toggle", Button).label.plain
                == "Auto-start: On"
            )

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
        app = DiskTideApp(
            scan_path=str(root),
            show_welcome=False,
            config=_config(),
            snapshot_repository=repository,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("2")
            await _settle(pilot, lambda: isinstance(app.screen, MonitorScreen))
            screen = app.screen
            assert isinstance(screen, MonitorScreen)
            await _wait_for_monitor_load(pilot, screen)
            await _settle(
                pilot,
                lambda: "no active host"
                in str(screen.query_one("#monitor-history-summary").render()),
            )
            assert screen.has_class("narrow")
            assert screen.query_one("#monitor-tabs").active == "monitor-history-tab"
            assert "no active host" in str(
                screen.query_one("#monitor-history-summary").render()
            )
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
            await _settle(pilot, lambda: screen.has_class("detail"))
            assert screen.has_class("detail")
            await pilot.press("escape")
            await _settle(pilot, lambda: not screen.has_class("detail"))
            assert not screen.has_class("detail")

            await pilot.press("s")
            await _settle(pilot, lambda: app._monitor_service.session_running)
            assert app._monitor_service.session_running
            await pilot.press("1")
            await _settle(pilot, lambda: isinstance(app.screen, ExplorerScreen))
            assert isinstance(app.screen, ExplorerScreen)
            assert app._monitor_service.session_running
            previous_dashboard = screen._dashboard
            app._monitor_service.stop_session(wait=True)
            await _settle(
                pilot, lambda: screen._dashboard is not previous_dashboard
            )
            await _drain_monitor_loads(pilot, app)
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
        app = DiskTideApp(
            scan_path=str(root),
            show_welcome=False,
            config=_config(),
            snapshot_repository=repository,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree")
            await pilot.press("down")
            await _settle(
                pilot,
                lambda: getattr(tree.cursor_node.data, "path", None) == str(selected),
            )
            assert tree.cursor_node.data.path == str(selected)

            await pilot.press("shift+m")
            await _settle(pilot, lambda: isinstance(app.screen, MonitorEditor))
            assert isinstance(app.screen, MonitorEditor)
            assert app.screen.query_one("#monitor-path").value == str(selected)

            await pilot.press("ctrl+s")
            await _settle(pilot, lambda: isinstance(app.screen, ExplorerScreen))
            assert isinstance(app.screen, ExplorerScreen)
            await _settle(
                pilot, lambda: len(app._monitor_service.list_monitors()) == 1
            )
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
