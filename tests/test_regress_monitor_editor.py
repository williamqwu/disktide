"""Regression for the monitor editor accepting a root it could never scan."""

from __future__ import annotations


# --- a monitor root the editor could never scan ------------------------


def test_the_monitor_editor_refuses_a_path_that_is_not_a_directory(tmp_path):
    from textual.app import App
    from textual.widgets import Input, Static

    from disktide.config import AppConfig
    from disktide.widgets.monitor_editor import MonitorEditor

    missing = str(tmp_path / "nope")

    async def scenario():
        app = App()
        async with app.run_test() as pilot:
            saved: list = []
            app.push_screen(MonitorEditor(config=AppConfig()), saved.append)
            await pilot.pause()
            app.screen.query_one("#monitor-path", Input).value = missing
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert not saved
            error = app.screen.query_one("#monitor-editor-error", Static)
            assert missing in error.render().plain

    import asyncio

    asyncio.run(scenario())
