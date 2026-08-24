"""CleanupPlan modal and TUI action-routing tests."""

from __future__ import annotations

import asyncio

from textual.app import App

from disktide.cleanup.detector import detect_targets
from disktide.domain.cleanup import CleanupActionKind
from disktide.repositories.sqlite import SQLiteSnapshotRepository
from disktide.scanner.walker import scan_directory
from disktide.screens.cleanup import CleanupScreen
from disktide.services.cleanup import CleanupService
from disktide.widgets.cleanup_modal import CleanupModal, CleanupModalResult


def _plan(tmp_path):
    root = tmp_path / "root"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"cache")
    repository = SQLiteSnapshotRepository(str(tmp_path / "cleanup.db"))
    repository.connect()
    service = CleanupService(repository)
    plan = service.create_plan(
        root,
        detect_targets(scan_directory(str(root))),
        provenance="test",
    )
    return root, cache, repository, service, plan


def test_cleanup_screen_preview_does_not_execute(monkeypatch, tmp_path):
    _, cache, repository, service, plan = _plan(tmp_path)
    screen = CleanupScreen(service=service)
    screen._current_plan_id = plan.id
    calls = []
    monkeypatch.setattr(
        screen,
        "_execute_plan",
        lambda *args: calls.append(args),
    )
    monkeypatch.setattr(screen, "notify", lambda *args, **kwargs: None)

    screen._on_modal_result(CleanupModalResult(CleanupActionKind.PREVIEW))

    assert calls == []
    assert cache.exists()
    repository.close()


def test_cleanup_screen_routes_safe_apply(monkeypatch, tmp_path):
    _, _, repository, service, plan = _plan(tmp_path)
    screen = CleanupScreen(service=service)
    screen._current_plan_id = plan.id
    calls = []
    monkeypatch.setattr(
        screen,
        "_execute_plan",
        lambda *args: calls.append(args),
    )

    screen._on_modal_result(CleanupModalResult(CleanupActionKind.TRASH))

    assert calls == [(plan.id, CleanupActionKind.TRASH, None)]
    repository.close()


def test_preview_button_returns_preview_action(tmp_path):
    _, _, repository, _, plan = _plan(tmp_path)

    class ModalApp(App):
        def __init__(self):
            super().__init__()
            self.result = None

        def on_mount(self) -> None:
            self.push_screen(
                CleanupModal(plan),
                callback=lambda result: setattr(self, "result", result),
            )

    async def run() -> None:
        app = ModalApp()
        async with app.run_test(size=(110, 44)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, CleanupModal)
            await pilot.click("#btn-preview")
            await pilot.pause()
            assert app.result == CleanupModalResult(CleanupActionKind.PREVIEW)

    try:
        asyncio.run(run())
    finally:
        repository.close()


def test_permanent_button_requires_exact_plan_token(tmp_path):
    _, _, repository, _, plan = _plan(tmp_path)

    class ModalApp(App):
        def __init__(self):
            super().__init__()
            self.result = None

        def on_mount(self) -> None:
            self.push_screen(
                CleanupModal(plan),
                callback=lambda result: setattr(self, "result", result),
            )

    async def run() -> None:
        app = ModalApp()
        async with app.run_test(size=(110, 44)) as pilot:
            await pilot.pause()
            await pilot.click("#btn-permanent")
            await pilot.pause()
            assert app.result is None
            await pilot.click("#confirm-input")
            await pilot.press(*CleanupService.permanent_confirmation(plan.id))
            await pilot.click("#btn-permanent")
            await pilot.pause()
            assert app.result == CleanupModalResult(
                CleanupActionKind.PERMANENT,
                CleanupService.permanent_confirmation(plan.id),
            )

    try:
        asyncio.run(run())
    finally:
        repository.close()
