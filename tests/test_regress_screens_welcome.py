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
