"""Tests for symbolic-link handling in the scanner and explorer."""

from __future__ import annotations

import asyncio
import os
from io import StringIO
from pathlib import Path

from rich.console import Console

from fs_monitor.app import FSMonitorApp
from fs_monitor.config import load_config
from fs_monitor.models.tree import FSNode
from fs_monitor.scanner.engine import ScanEngine
from fs_monitor.scanner.walker import classify_symlink
from fs_monitor.screens.explorer import ExplorerScreen
from fs_monitor.widgets.info_panel import InfoPanel
from fs_monitor.widgets.size_tree import SizeTree


def _find(node: FSNode, name: str) -> FSNode | None:
    for child in node.children:
        if child.name == name:
            return child
    return None


# --- scanner: symlink classification --------------------------------------


def test_symlink_to_dir_is_detected(tmp_path):
    real = tmp_path / "realdir"
    real.mkdir()
    (real / "f.txt").write_text("hello")
    os.symlink(real, tmp_path / "link")

    root = ScanEngine(workers=1).scan(str(tmp_path))
    link = _find(root, "link")
    assert link is not None
    assert link.is_symlink is True
    assert link.is_dir is False          # never a directory in the tree
    # Classification is lazy now; the UI calls classify_symlink on
    # demand. We call it explicitly here to verify the result.
    classify_symlink(link)
    assert link.link_is_dir is True
    assert link.link_broken is False
    assert link.symlink_to_dir is True
    assert link.link_target == str(real)


def test_symlink_to_file_is_detected(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("data")
    os.symlink(target, tmp_path / "link")

    root = ScanEngine(workers=1).scan(str(tmp_path))
    link = _find(root, "link")
    assert link.is_symlink is True
    classify_symlink(link)
    assert link.link_is_dir is False
    assert link.link_broken is False
    assert link.symlink_to_dir is False


def test_broken_symlink_is_detected(tmp_path):
    os.symlink(tmp_path / "does-not-exist", tmp_path / "dangling")

    root = ScanEngine(workers=1).scan(str(tmp_path))
    link = _find(root, "dangling")
    assert link.is_symlink is True
    classify_symlink(link)
    assert link.link_broken is True
    assert link.link_is_dir is False


def test_symlink_to_dir_is_not_recursed(tmp_path):
    """The link is sized by itself; the target's bytes do not roll up."""
    real = tmp_path / "realdir"
    real.mkdir()
    (real / "big.bin").write_bytes(b"x" * 50_000)
    os.symlink(real, tmp_path / "link")

    root = ScanEngine(workers=1).scan(str(tmp_path))
    link = _find(root, "link")
    assert not link.children            # target contents not pulled in
    assert link.size < 1_000            # just the link itself
    # The 50 KB is counted once (the real directory), not twice.
    assert 50_000 <= root.size < 60_000


def test_top_level_symlink_counts_as_one_file(tmp_path):
    """A symlink directly under the scan root is counted (engine path)."""
    (tmp_path / "real.txt").write_text("x")
    os.symlink(tmp_path / "real.txt", tmp_path / "link")

    root = ScanEngine(workers=1).scan(str(tmp_path))
    assert root.file_count == 2         # real.txt + link


def test_nested_symlink_counts_as_one_file(tmp_path):
    """A symlink inside a subdirectory is counted (walker path)."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "real.txt").write_text("x")
    os.symlink(sub / "real.txt", sub / "link")

    root = ScanEngine(workers=1).scan(str(tmp_path))
    subdir = _find(root, "sub")
    assert subdir.file_count == 2       # real.txt + link


# --- model + rendering ----------------------------------------------------


def test_symlink_to_dir_property():
    assert FSNode(name="l", path="/l", is_symlink=True, link_is_dir=True).symlink_to_dir
    assert not FSNode(name="l", path="/l", is_symlink=True, link_is_dir=False).symlink_to_dir
    assert not FSNode(name="d", path="/d", is_dir=True).symlink_to_dir


def test_size_tree_label_shows_symlink_target():
    node = FSNode(
        name="mylink", path="/x/mylink", size=20, is_symlink=True,
        link_target="/some/target", link_is_dir=True,
    )
    label = SizeTree(node)._make_label(node).plain
    assert "mylink" in label
    assert "→" in label
    assert "/some/target/" in label     # trailing slash: target is a directory


def test_size_tree_label_marks_broken_symlink():
    node = FSNode(
        name="dead", path="/x/dead", size=10, is_symlink=True,
        link_target="/gone", link_broken=True,
    )
    label = SizeTree(node)._make_label(node).plain
    assert "(broken)" in label


def test_info_panel_shows_symlink_target():
    # link_classified=True so the InfoPanel's on-demand classify_symlink
    # is a no-op for this fake path; the manually-set link_is_dir holds.
    node = FSNode(
        name="link", path="/x/link", size=12, is_symlink=True,
        link_target="/real/dir", link_is_dir=True, link_classified=True,
    )
    panel = InfoPanel()
    panel._display = type(
        "Stub", (), {"update": lambda self, x: setattr(self, "last", x)}
    )()
    panel.update_node(node)
    buf = StringIO()
    Console(file=buf, width=200, color_system=None).print(panel._display.last)
    out = buf.getvalue()
    assert "Symlink" in out
    assert "/real/dir" in out
    assert "Directory" in out           # target type


# --- explorer: `i` navigates a symlinked directory ------------------------


async def _wait_for_explorer(pilot, app) -> None:
    await pilot.pause(delay=0.2)
    for _ in range(30):
        await pilot.pause(delay=0.1)
        if isinstance(app.screen, ExplorerScreen) and app.screen._root is not None:
            return


async def _cursor_to(pilot, tree, name: str) -> bool:
    """Move the tree cursor onto the first node whose FSNode name matches."""
    for _ in range(tree.last_line + 1):
        node = tree.cursor_node
        if node is not None and node.data is not None and node.data.name == name:
            return True
        await pilot.press("down")
        await pilot.pause()
    node = tree.cursor_node
    return node is not None and node.data is not None and node.data.name == name


def test_press_i_enters_symlinked_directory(tmp_path):
    real = tmp_path / "realdir"
    real.mkdir()
    (real / "inside.txt").write_text("x" * 5000)
    os.symlink(real, tmp_path / "link")
    resolved = str(Path(real).resolve())

    async def go():
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree", SizeTree)

            assert await _cursor_to(pilot, tree, "link")
            assert tree.cursor_node.data.is_symlink

            await pilot.press("i")
            for _ in range(30):
                await pilot.pause(delay=0.1)
                if screen._root is not None and screen._root.path == resolved:
                    break
            # `i` resolved the link and rescanned from the real directory.
            assert screen._scan_path == resolved
            assert screen._root.path == resolved

    asyncio.run(go())


def test_press_i_does_nothing_on_file_symlink(tmp_path):
    (tmp_path / "data.txt").write_text("x" * 5000)
    os.symlink(tmp_path / "data.txt", tmp_path / "flink")

    async def go():
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree", SizeTree)

            assert await _cursor_to(pilot, tree, "flink")
            before = screen._scan_path
            await pilot.press("i")
            await pilot.pause()
            # A symlink whose target is a file is not enterable.
            assert screen._scan_path == before

    asyncio.run(go())


# --- scanner: lazy symlink target classification --------------------------


def test_symlinks_are_not_classified_during_scan(tmp_path):
    """No symlink, top-level or deeper, is classified during the scan.

    The walker pays exactly one syscall per symlink (entry.stat for the
    link's own size); link_target / link_is_dir / link_broken stay at
    defaults until something (the UI, a test, ...) calls classify_symlink.
    This is what restores v0.1.3-equivalent scan speed on symlink-heavy
    NFS trees where every saved syscall is a server round-trip.
    """
    target = tmp_path / "real"
    target.mkdir()
    sub = tmp_path / "sub"
    sub.mkdir()
    os.symlink(target, tmp_path / "top_link")   # at scan root
    os.symlink(target, sub / "deep_link")       # one level deeper

    root = ScanEngine(workers=1).scan(str(tmp_path))
    top = _find(root, "top_link")
    deep = _find(_find(root, "sub"), "deep_link")

    for link in (top, deep):
        assert link is not None and link.is_symlink is True
        assert link.link_classified is False
        # Defaults, not the truth yet, just the not-classified state.
        assert link.link_target is None
        assert link.link_is_dir is False
        assert link.link_broken is False


def test_classify_symlink_fills_in_a_deferred_link(tmp_path):
    """An unclassified symlink becomes classified after classify_symlink,
    and that one call also captures the target text (readlink), so the
    UI does not need a second pass.
    """
    target = tmp_path / "real"
    target.mkdir()
    os.symlink(target, tmp_path / "link")

    root = ScanEngine(workers=1).scan(str(tmp_path))
    link = _find(root, "link")
    assert link.link_classified is False
    assert link.link_target is None

    classify_symlink(link)
    assert link.link_classified is True
    assert link.link_is_dir is True
    assert link.link_target == str(target)


def test_classify_symlink_is_idempotent(tmp_path):
    """A second classify_symlink call does not re-stat (proved by
    mutating link_is_dir after the first call and checking it stays)."""
    target = tmp_path / "real"
    target.mkdir()
    os.symlink(target, tmp_path / "link")

    root = ScanEngine(workers=1).scan(str(tmp_path))
    link = _find(root, "link")
    classify_symlink(link)               # first call: real classification
    assert link.link_classified is True
    link.link_is_dir = False             # pretend we mutated state
    classify_symlink(link)               # second call: no-op
    assert link.link_is_dir is False     # unchanged — no re-stat
