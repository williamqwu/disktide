"""What happens between the last directory read and the finished chart.

Every rule here was measured on a 679k-node home directory (86,631 dirs /
592,777 files) whose post-walk tail ran ~18 s: 10.7 s cloning the tree,
5.3 s inside Textual's `@work` decorator repr-ing an `FSNode` argument,
0.9 s on hardlink accounting, 0.7 s sorting a walk to report zero access
errors, and 0.45 s of a wholly neutral chart before the category tints
landed. Only the first and third of those are real work.
"""

from __future__ import annotations

import time
from pathlib import Path

from textual import work
from textual.app import App

from disktide.domain.metrics import MetricId
from disktide.domain.policy import ScanPolicy
from disktide.domain.scan import (
    AccessError,
    NodeAggregateUpdated,
    ScanPhase,
    ScanPhaseChanged,
    ScanRequest,
)
from disktide.models.tree import FSNode
from disktide.scanner.scheduler import clone_tree
from disktide.services.scan import ScanService
from disktide.services.scan_consumers import ScanEventRecorder


# --- clone_tree ------------------------------------------------------------


def _small_tree() -> FSNode:
    leaf = FSNode(name="a.bin", path="/r/d/a.bin", size=7, own_size=7)
    sub = FSNode(name="d", path="/r/d", size=7, is_dir=True, children=[leaf])
    top = FSNode(name="t.txt", path="/r/t.txt", size=3, own_size=3)
    return FSNode(name="r", path="/r", size=10, is_dir=True, children=[sub, top])


def test_clone_tree_copies_every_node_by_default():
    root = _small_tree()

    cloned = clone_tree(root)

    assert cloned is not root
    assert cloned.children[0] is not root.children[0]
    assert cloned.children[1] is not root.children[1]
    assert cloned.children[0].children[0] is not root.children[0].children[0]


def test_clone_tree_can_share_leaves_and_still_copy_directories():
    root = _small_tree()

    cloned = clone_tree(root, share_leaves=True)

    # Directories are what a published snapshot's aggregates hang off, so
    # they are copied and can no longer be reached from the original.
    assert cloned is not root
    assert cloned.children[0] is not root.children[0]
    # Leaves carry only their own final value, so they stay shared: that is
    # what takes the clone from every node to the 13% that are directories.
    assert cloned.children[1] is root.children[1]
    assert cloned.children[0].children[0] is root.children[0].children[0]


def test_shared_leaf_clone_preserves_shape_and_totals():
    root = _small_tree()

    cloned = clone_tree(root, share_leaves=True)

    assert [node.path for node in cloned.walk()] == [
        node.path for node in root.walk()
    ]
    assert cloned.size == root.size
    assert cloned.children[0].children[0].size == 7


def test_shared_leaf_clone_isolates_directory_aggregates():
    root = _small_tree()
    cloned = clone_tree(root, share_leaves=True)

    cloned.children[0].size = 999

    assert root.children[0].size == 7


# --- the finalizing phase --------------------------------------------------


def _real_tree(tmp_path: Path) -> None:
    for name in ("alpha", "beta"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "blob.bin").write_bytes(b"x" * 32)


def _request(tmp_path: Path, *, live: bool = True) -> ScanRequest:
    return ScanRequest(
        path=str(tmp_path),
        metric=MetricId.LOGICAL,
        policy=ScanPolicy(one_file_system=False, exclude_pseudo_filesystems=True),
        emit_tree_updates=live,
        source="test",
    )


def test_the_run_is_already_finalizing_while_the_clone_runs(tmp_path, monkeypatch):
    """The clone runs for seconds on a large tree; it must not read as walking."""
    _real_tree(tmp_path)
    phase_during_clone: list[ScanPhase] = []

    import disktide.scanner.engine as engine_mod

    original = engine_mod.clone_tree
    service = ScanService()
    run = service.create_run(_request(tmp_path))

    def spy(root, **kwargs):
        phase_during_clone.append(run.phase)
        return original(root, **kwargs)

    monkeypatch.setattr(engine_mod, "clone_tree", spy)
    service.execute(run, consumers=(ScanEventRecorder(),))

    assert phase_during_clone == [ScanPhase.FINALIZING]


def test_the_engine_calls_its_walk_hook_before_cloning(tmp_path, monkeypatch):
    _real_tree(tmp_path)
    order: list[str] = []

    import disktide.scanner.engine as engine_mod

    original = engine_mod.clone_tree
    monkeypatch.setattr(
        engine_mod,
        "clone_tree",
        lambda root, **kwargs: (order.append("clone"), original(root, **kwargs))[1],
    )

    engine = engine_mod.ScanEngine(
        workers=2,
        scan_path=str(tmp_path),
        tree_update_callback=lambda update: None,
        walk_complete_callback=lambda: order.append("hook"),
    )
    engine.scan(str(tmp_path))

    assert order[:2] == ["hook", "clone"]


def test_finalizing_is_announced_exactly_once(tmp_path):
    _real_tree(tmp_path)
    recorder = ScanEventRecorder()

    service = ScanService()
    run = service.scan(_request(tmp_path), consumers=(recorder,))

    finalizing = [
        event
        for event in recorder.events
        if isinstance(event, ScanPhaseChanged)
        and event.phase is ScanPhase.FINALIZING
    ]
    assert len(finalizing) == 1
    assert run.root is not None


def test_finalizing_still_announced_when_the_collector_has_no_hook(tmp_path):
    """A custom collector cannot call the hook, so the service still sets it."""
    _real_tree(tmp_path)
    root = FSNode(name=tmp_path.name, path=str(tmp_path), is_dir=True)

    class _Bare:
        cancelled = False

        def __init__(self, request, progress_callback, tree_callback):
            self._root = root

        def scan(self):
            return self._root

        def cancel(self):
            pass

    recorder = ScanEventRecorder()
    service = ScanService(scanner_factory=_Bare)
    service.scan(_request(tmp_path, live=False), consumers=(recorder,))

    assert any(
        isinstance(event, ScanPhaseChanged)
        and event.phase is ScanPhase.FINALIZING
        for event in recorder.events
    )


def test_final_aggregate_lands_after_the_phase_flip(tmp_path):
    _real_tree(tmp_path)
    recorder = ScanEventRecorder()

    ScanService().scan(_request(tmp_path), consumers=(recorder,))

    kinds = [type(event).__name__ for event in recorder.events]
    phase_at = next(
        index
        for index, event in enumerate(recorder.events)
        if isinstance(event, ScanPhaseChanged)
        and event.phase is ScanPhase.FINALIZING
    )
    final_at = next(
        index
        for index, event in enumerate(recorder.events)
        if isinstance(event, NodeAggregateUpdated) and event.final
    )
    assert phase_at < final_at, kinds


# --- access-error reporting ------------------------------------------------


def _denied_tree() -> FSNode:
    """A root with two unreadable directories among a crowd of files."""
    files = [
        FSNode(name=f"f{i}.bin", path=f"/r/f{i}.bin", size=1, own_size=1)
        for i in range(50)
    ]
    zulu = FSNode(name="zulu", path="/r/zulu", is_dir=True, error="Permission denied")
    alpha = FSNode(
        name="alpha", path="/r/alpha", is_dir=True, error="Permission denied"
    )
    root = FSNode(
        name="r",
        path="/r",
        is_dir=True,
        children=[*files, zulu, alpha],
        inaccessible_count=2,
        inaccessible_subtree_count=2,
    )
    return root


class _Emitter:
    def __init__(self):
        self.emitted: list[tuple[str, str, int]] = []

    def emit(self, event_type, **payload):
        self.emitted.append(
            (payload["path"], payload["message"], payload["count"])
        )
        return None


def test_access_errors_are_reported_in_path_order():
    emitter = _Emitter()

    ScanService._emit_access_errors(_denied_tree(), emitter)

    assert [entry[0] for entry in emitter.emitted] == ["/r/alpha", "/r/zulu"]
    assert all(entry[1] == "Permission denied" for entry in emitter.emitted)


def test_unrepresented_direct_entries_are_reported_against_the_parent():
    root = FSNode(
        name="r",
        path="/r",
        is_dir=True,
        inaccessible_count=3,
        inaccessible_subtree_count=3,
    )
    emitter = _Emitter()

    ScanService._emit_access_errors(root, emitter)

    assert emitter.emitted == [("/r", "unreadable direct entries", 3)]


def test_a_clean_tree_reports_nothing():
    root = FSNode(
        name="r",
        path="/r",
        is_dir=True,
        children=[
            FSNode(name=f"f{i}", path=f"/r/f{i}", size=1, own_size=1)
            for i in range(200)
        ],
    )
    emitter = _Emitter()

    ScanService._emit_access_errors(root, emitter)

    assert emitter.emitted == []


def test_file_level_errors_are_still_reported():
    """Only directories usually fail, but the walker may mark a leaf too."""
    leaf = FSNode(name="f", path="/r/f", error="Stale file handle")
    root = FSNode(name="r", path="/r", is_dir=True, children=[leaf])
    emitter = _Emitter()

    ScanService._emit_access_errors(root, emitter)

    assert emitter.emitted == [("/r/f", "Stale file handle", 1)]


# --- Textual's @work argument repr -----------------------------------------


class _Tripwire:
    """Stands in for an FSNode: reprs are the thing under test."""

    def __init__(self):
        self.reprs = 0

    def __repr__(self) -> str:  # pragma: no cover - counted, not compared
        self.reprs += 1
        return "<tripwire>"


def test_work_without_a_description_reprs_its_arguments():
    """Guards the premise: this is the cost the real decorator must avoid."""
    seen: list[int] = []

    class Probe(App):
        @work(thread=True, group="probe-bare")
        def go(self, payload: _Tripwire) -> None:
            pass

        async def on_mount(self) -> None:
            tripwire = _Tripwire()
            self.go(tripwire)
            seen.append(tripwire.reprs)
            self.exit()

    Probe().run(headless=True)
    assert seen == [1]


def test_work_with_a_description_never_reprs_its_arguments():
    seen: list[int] = []

    class Probe(App):
        @work(thread=True, group="probe-named", description="named")
        def go(self, payload: _Tripwire) -> None:
            pass

        async def on_mount(self) -> None:
            tripwire = _Tripwire()
            self.go(tripwire)
            seen.append(tripwire.reprs)
            self.exit()

    Probe().run(headless=True)
    assert seen == [0]


def test_every_worker_taking_a_tree_declares_a_description():
    """An FSNode repr renders the whole subtree; none may reach the decorator."""
    import ast

    offenders: list[str] = []
    for path in sorted(Path("src/disktide").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            takes_node = any(
                isinstance(arg.annotation, ast.Name)
                and arg.annotation.id == "FSNode"
                or isinstance(arg.annotation, ast.BinOp)
                and "FSNode" in ast.unparse(arg.annotation)
                for arg in node.args.args
                if arg.annotation is not None
            )
            if not takes_node:
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if getattr(decorator.func, "id", None) != "work":
                    continue
                named = {kw.arg for kw in decorator.keywords}
                if "description" not in named:
                    offenders.append(f"{path}:{node.lineno} {node.name}")
    assert offenders == []


# --- the rollup that keeps a live chart from being one flat colour ---------


def test_category_rollup_reports_its_own_cost_and_clears_the_gate():
    from disktide.screens.explorer import ExplorerScreen

    screen = ExplorerScreen.__new__(ExplorerScreen)
    screen._category_index_running = True
    screen._category_index_cost = 0.0
    screen._category_index_at = 0.0

    screen._settle_category_index(0.25)

    assert screen._category_index_running is False
    assert screen._category_index_cost == 0.25
    assert screen._category_index_at > 0.0


def test_category_rollup_backs_off_by_its_own_cost(monkeypatch):
    """A rollup that costs 0.4 s may not run again for several seconds."""
    from disktide.screens.explorer import ExplorerScreen

    dispatched: list[FSNode] = []
    screen = ExplorerScreen.__new__(ExplorerScreen)
    screen._category_index_running = False
    screen._category_index_cost = 0.4
    screen._category_index_at = time.monotonic()
    monkeypatch.setattr(
        ExplorerScreen,
        "_category_index_worker",
        lambda self, root: dispatched.append(root),
    )
    root = _small_tree()

    screen._maybe_build_category_index(root)
    assert dispatched == []

    screen._category_index_at = time.monotonic() - (
        0.4 * ExplorerScreen._CATEGORY_INDEX_DUTY + 0.1
    )
    screen._maybe_build_category_index(root)
    assert dispatched == [root]
    assert screen._category_index_running is True


def test_category_rollup_never_runs_two_passes_at_once():
    from disktide.screens.explorer import ExplorerScreen

    screen = ExplorerScreen.__new__(ExplorerScreen)
    screen._category_index_running = True
    screen._category_index_cost = 0.0
    screen._category_index_at = 0.0

    # Would otherwise be long past due; the in-flight pass still wins.
    screen._maybe_build_category_index(_small_tree())


# --- the live chart --------------------------------------------------------


def _live_event(root: FSNode, view_root):
    from disktide.domain.scan import NodeAggregateUpdated as _Update

    return _Update(
        run_id="run",
        sequence=1,
        phase=ScanPhase.SCANNING,
        root=root,
        final=False,
        changed_nodes=(root,),
        view_root=view_root,
    )


def _live_tree(tmp_path: Path) -> None:
    for name in ("alpha", "beta"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "blob.bin").write_bytes(b"x" * 4096)


def test_a_live_frame_reuses_the_view_the_scheduler_shipped(tmp_path):
    """The scheduler builds the bounded view off the scan thread; rebuilding
    it on the UI thread cost 2.07 s across one 100 s scan for nothing."""
    import asyncio

    from disktide.app import DiskTideApp
    from disktide.config import load_config
    from disktide.domain.live_view import build_live_view
    from disktide.screens.explorer import ExplorerScreen
    from disktide.widgets.size_tree import SizeTree
    import disktide.screens.explorer as explorer_mod

    _live_tree(tmp_path)

    async def go():
        config = load_config()
        config.ui.default_viz = "sunburst"
        app = DiskTideApp(scan_path=str(tmp_path), show_welcome=False, config=config)
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(30):
                await pilot.pause(delay=0.1)
                if (
                    isinstance(app.screen, ExplorerScreen)
                    and app.screen._root is not None
                ):
                    break
            screen = app.screen
            root = screen._root
            shipped = build_live_view(root, metric=MetricId.LOGICAL)
            rebuilt: list[object] = []
            original = explorer_mod.build_live_view
            explorer_mod.build_live_view = lambda *a, **k: (
                rebuilt.append(a) or original(*a, **k)
            )
            try:
                screen._scan_in_progress = True
                screen._active_metric = MetricId.LOGICAL
                screen._apply_tree_snapshot(_live_event(root, shipped))
                assert rebuilt == []
                assert screen._live_view_snapshot is shipped

                # A metric switch invalidates it: the shipped view is
                # weighted by whatever the run was started with.
                screen.query_one("#size-tree", SizeTree).metric = "files"
                screen._apply_tree_snapshot(_live_event(root, shipped))
                assert rebuilt
                assert screen._live_view_snapshot is not shipped
            finally:
                explorer_mod.build_live_view = original
                screen._scan_in_progress = False

    asyncio.run(go())


def test_a_live_frame_asks_for_a_category_rollup(tmp_path):
    """Without one every directory arc is the same neutral colour, which on
    a 256-colour terminal left a 74-arc disc drawn in five."""
    import asyncio

    from disktide.app import DiskTideApp
    from disktide.config import load_config
    from disktide.domain.live_view import build_live_view
    from disktide.screens.explorer import ExplorerScreen

    _live_tree(tmp_path)

    async def go():
        config = load_config()
        config.ui.default_viz = "sunburst"
        app = DiskTideApp(scan_path=str(tmp_path), show_welcome=False, config=config)
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(30):
                await pilot.pause(delay=0.1)
                if (
                    isinstance(app.screen, ExplorerScreen)
                    and app.screen._root is not None
                ):
                    break
            screen = app.screen
            root = screen._root
            asked: list[FSNode] = []
            screen._maybe_build_category_index = asked.append
            screen._scan_in_progress = True
            screen._active_metric = MetricId.LOGICAL
            screen._apply_tree_snapshot(
                _live_event(root, build_live_view(root, metric=MetricId.LOGICAL))
            )
            screen._scan_in_progress = False

            assert asked == [root]

    asyncio.run(go())


def test_a_finished_scan_has_its_tints_before_the_chart_is_built(tmp_path):
    """Completion used to paint one full-depth neutral frame and recolour
    ~0.45 s later, which read as a second jump."""
    import asyncio

    from disktide.app import DiskTideApp
    from disktide.config import load_config
    from disktide.screens.explorer import ExplorerScreen

    _live_tree(tmp_path)

    async def go():
        config = load_config()
        config.ui.default_viz = "sunburst"
        app = DiskTideApp(scan_path=str(tmp_path), show_welcome=False, config=config)
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(30):
                await pilot.pause(delay=0.1)
                if (
                    isinstance(app.screen, ExplorerScreen)
                    and app.screen._root is not None
                ):
                    break
            screen = app.screen
            order: list[str] = []
            screen._build_category_index = lambda root: order.append("rollup")
            original = screen._update_active_viz
            screen._update_active_viz = lambda *a, **k: (
                order.append("chart") or original(*a, **k)
            )
            screen._active_run = None
            screen._on_scan_complete(screen._root)

            assert order.index("rollup") < order.index("chart")

    asyncio.run(go())
