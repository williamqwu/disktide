"""Cleanup modal action routing tests."""

from __future__ import annotations

import asyncio

from textual.app import App

from fs_monitor.models.patterns import CleanupRule, CleanupTarget
from fs_monitor.screens.cleanup import CleanupScreen
from fs_monitor.widgets.cleanup_modal import CleanupModal


def _target(path: str) -> CleanupTarget:
    return CleanupTarget(
        path=path,
        size=128,
        rule=CleanupRule(name="test", description="", patterns=[]),
    )


def test_cleanup_screen_routes_dry_run_without_deletion(monkeypatch, tmp_path):
    path = tmp_path / "candidate"
    path.write_text("keep me")
    target = _target(str(path))
    screen = CleanupScreen()
    screen._targets = [target]
    screen._selected = {target.path}
    calls = []

    monkeypatch.setattr(
        screen,
        "_do_delete",
        lambda targets, dry_run=False: calls.append((targets, dry_run)),
    )

    screen._on_modal_result(CleanupModal.DRY_RUN)

    assert calls == [([target], True)]
    assert path.exists()


def test_dry_run_button_returns_dry_run_action(tmp_path):
    target = _target(str(tmp_path / "candidate"))

    class ModalApp(App):
        def __init__(self):
            super().__init__()
            self.result: str | None = None

        def on_mount(self) -> None:
            self.push_screen(
                CleanupModal([target]),
                callback=lambda result: setattr(self, "result", result),
            )

    async def run() -> None:
        app = ModalApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, CleanupModal)
            await pilot.click("#btn-dry-run")
            await pilot.pause()
            assert app.result == CleanupModal.DRY_RUN

    asyncio.run(run())
