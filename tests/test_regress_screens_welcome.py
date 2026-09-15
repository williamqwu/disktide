"""The welcome screen's path box must survive a path it cannot even stat.

`Path.is_dir()` only swallows the "this is not a directory" family of
errors; an unsearchable parent (EACCES) or an over-long name (ENAMETOOLONG)
comes back out as an exception.  Raised inside an `Input.Submitted`
handler that is Textual's fatal path, so a typo used to take the whole TUI
down with a traceback instead of raising a toast.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from textual.widgets import Input

from disktide.app import DiskTideApp
from disktide.config import AppConfig
from disktide.screens.welcome import WelcomeScreen


def _isolate(monkeypatch, tmp_path):
    """Keep the app's config and database inside the test's own tmp dir."""
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))


def _submit(value: str):
    """Type `value` into the welcome path box and press enter."""

    async def go() -> tuple[bool, object]:
        app = DiskTideApp(show_welcome=True, config=AppConfig())
        async with app.run_test(size=(100, 40)) as pilot:
            for _ in range(40):
                if isinstance(app.screen, WelcomeScreen):
                    break
                await pilot.pause(delay=0.05)
            assert isinstance(app.screen, WelcomeScreen)
            app.screen.query_one("#path-input", Input).value = value
            await pilot.press("enter")
            await pilot.pause()
            return isinstance(app.screen, WelcomeScreen), app.return_value

    return asyncio.run(go())


class TestUnreadableWelcomePath:
    def test_a_directory_we_may_not_search_toasts(self, monkeypatch, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root can search a 0o000 directory")
        _isolate(monkeypatch, tmp_path)
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "data").mkdir()
        os.chmod(locked, 0o000)
        try:
            still_welcome, result = _submit(str(locked / "data"))
        finally:
            os.chmod(locked, 0o700)
        assert still_welcome
        assert result is None

    def test_an_over_long_path_toasts(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path)
        still_welcome, result = _submit("/" + "a" * 5000)
        assert still_welcome
        assert result is None

    def test_a_real_directory_is_still_accepted(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path)
        target = tmp_path / "ok"
        target.mkdir()
        still_welcome, _ = _submit(str(target))
        assert not still_welcome


class TestModeKeysOnTheWelcomeScreen:
    """`1`/`2`/`3`/`4` and `,` used to take the app down before a path.

    The four mode digits and the settings comma are app-level bindings, so
    they are live from mount, but the screens they name are installed only
    once a path has been chosen. Pressing one after Tab moved focus off the
    path box raised `No screen called 'explorer' installed` -- or, for the
    two modes that read the explorer, an `AttributeError` -- out of the key
    handler, which is Textual's fatal path.
    """

    @staticmethod
    def _press(monkeypatch, tmp_path, keys):
        _isolate(monkeypatch, tmp_path)

        async def go():
            config = AppConfig()
            config.ui.show_cleanup = True
            app = DiskTideApp(show_welcome=True, config=config)
            try:
                async with app.run_test(size=(100, 40)) as pilot:
                    for _ in range(40):
                        if isinstance(app.screen, WelcomeScreen):
                            break
                        await pilot.pause(delay=0.05)
                    assert isinstance(app.screen, WelcomeScreen)
                    await pilot.press("tab")
                    survived = []
                    for key in keys:
                        await pilot.press(key)
                        await pilot.pause()
                        survived.append(
                            (
                                key,
                                app.is_running,
                                app.return_code,
                                type(app.screen).__name__,
                            )
                        )
                    return survived
            finally:
                service = app.__dict__.get("_DiskTideApp__monitor_service")
                if service is not None:
                    service.stop_session(wait=False)

        return asyncio.run(go())

    @pytest.mark.parametrize("key", ["1", "2", "3", "4", "comma"])
    def test_a_mode_key_before_a_path_leaves_the_app_alive(
        self, monkeypatch, tmp_path, key
    ):
        (result,) = self._press(monkeypatch, tmp_path, [key])

        assert result == (key, True, None, "WelcomeScreen")

    def test_the_welcome_footer_does_not_advertise_the_mode_keys(
        self, monkeypatch, tmp_path
    ):
        _isolate(monkeypatch, tmp_path)

        async def go():
            app = DiskTideApp(show_welcome=True, config=AppConfig())
            async with app.run_test(size=(100, 40)) as pilot:
                for _ in range(40):
                    if isinstance(app.screen, WelcomeScreen):
                        break
                    await pilot.pause(delay=0.05)
                # Off the path box, which otherwise swallows every digit.
                await pilot.press("tab")
                await pilot.pause()
                return {
                    key: binding.action
                    for key, (_, binding, enabled, _) in
                    app.screen.active_bindings.items()
                    if enabled
                }

        bindings = asyncio.run(go())

        assert not [key for key in bindings if key in {"1", "2", "3", "4"}]
        assert "comma" not in bindings
        # The keys that do work on the welcome screen are still offered.
        assert bindings.get("q") == "quit"
