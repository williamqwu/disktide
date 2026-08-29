"""The tree's bar+percent tail must fit the panel instead of being cropped.

Textual crops a Tree label at the panel edge, so a fixed-width bar plus
percent on a deeply-indented row used to render as a fragment ("16." /
"0.2" with the digits and sign shorn off). SizeTree now measures the room
left on each row and degrades the bar before the percent.
"""

from __future__ import annotations

import asyncio
import re

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.rendering import bar_chars
from disktide.screens.explorer import ExplorerScreen
from disktide.widgets.size_tree import SizeTree

# A percent that survived the crop intact: digits, a decimal, one digit, "%".
COMPLETE_PCT = re.compile(r"\d+\.\d%")
# The classic clipped tails: "…16." and "…0.2" with nothing after them.
FRAGMENT = re.compile(r"\d+\.$|\d+\.\d$")


async def _wait_for_explorer(pilot, app) -> None:
    await pilot.pause(delay=0.2)
    for _ in range(40):
        await pilot.pause(delay=0.1)
        if isinstance(app.screen, ExplorerScreen) and app.screen._root is not None:
            return


def _make_clipping_tree(tmp_path) -> None:
    """A tree whose rows overflow a 52-column panel.

    `tests/` totals ~614 KiB for a two-digit share of the root; the
    `presentation/tui/viewmodels/` chain is deep enough that its guide
    indentation alone eats most of the row while its share rounds to
    0.2%, which is where the original clipping was first spotted.
    """
    tests = tmp_path / "tests"
    tests.mkdir()
    for i in range(7):
        (tests / f"test_module_{i:02d}.py").write_text("x" * 89_834)

    src = tmp_path / "src" / "disktide"
    src.mkdir(parents=True)
    for i in range(11):
        (src / f"module_{i:02d}.py").write_text("y" * 250_000)

    deep = src / "presentation" / "tui" / "viewmodels"
    deep.mkdir(parents=True)
    (deep / "v.py").write_text("z" * 7_000)

    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(4):
        (docs / f"guide_{i}.md").write_text("w" * 120_000)


async def _expand_everything(tree: SizeTree, pilot, rounds: int = 6) -> None:
    """Open every materialized node, letting lazy population catch up."""
    for _ in range(rounds):
        stack = [tree.root]
        while stack:
            node = stack.pop()
            if node.allow_expand and not node.is_expanded:
                node.expand()
            stack.extend(node.children)
        await pilot.pause(delay=0.05)
    await pilot.pause(delay=0.1)


def _visible_rows(tree: SizeTree) -> list[str]:
    """Reconstruct what each tree row actually shows on screen.

    `render_line` crops to the content width, which still includes the
    columns a vertical scrollbar would paint over, so crop again to the
    scrollable region — that is the width the user really sees.
    """
    width = tree.scrollable_content_region.width
    rows = []
    for y in range(min(tree.last_line + 1, tree.size.height)):
        rows.append(tree.render_line(y).crop(0, width).text.rstrip())
    return rows


def _assert_no_clipped_tail(rows: list[str], width: int) -> None:
    filled_ch, empty_ch = bar_chars()
    bar_glyphs = {filled_ch, empty_ch} - {" "}
    checked = 0
    for row in rows:
        has_tail = "%" in row or any(ch in row for ch in bar_glyphs)
        if not has_tail:
            # File rows carry no bar or percent; their own truncation is a
            # separate concern from the tail this fix owns.
            continue
        checked += 1
        assert COMPLETE_PCT.search(row), (
            f"row at width {width} shows a bar or percent but no complete "
            f"percent: {row!r}"
        )
        assert not FRAGMENT.search(row), (
            f"row at width {width} ends with a clipped percent fragment: {row!r}"
        )
    assert checked >= 4, f"expected several bar rows to check, got {checked}"


async def _rows_at(tmp_path, size) -> tuple[list[str], int]:
    app = DiskTideApp(scan_path=str(tmp_path), show_welcome=False, config=load_config())
    async with app.run_test(size=size) as pilot:
        await _wait_for_explorer(pilot, app)
        tree = app.screen.query_one("#size-tree", SizeTree)
        await pilot.pause(delay=0.2)
        await _expand_everything(tree, pilot)
        return _visible_rows(tree), tree.scrollable_content_region.width


def test_tree_rows_never_show_a_clipped_percent(tmp_path):
    _make_clipping_tree(tmp_path)

    async def go():
        rows, width = await _rows_at(tmp_path, (130, 38))
        _assert_no_clipped_tail(rows, width)

        tests_row = next((r for r in rows if "tests/" in r), None)
        assert tests_row is not None, f"no tests/ row rendered; rows={rows}"
        # Two digits before the decimal: the share the clip used to eat.
        assert re.search(r"\s\d\d\.\d%$", tests_row), (
            f"tests/ row lost its full percent: {tests_row!r}"
        )

        deep_row = next((r for r in rows if "viewmodels/" in r), None)
        assert deep_row is not None, f"no viewmodels/ row rendered; rows={rows}"
        # The deepest row is where the bar has to give way to the percent.
        assert deep_row.endswith("%"), (
            f"deep row kept a bar at the percent's expense: {deep_row!r}"
        )

    asyncio.run(go())


def test_tree_rows_never_show_a_clipped_percent_when_narrow(tmp_path):
    _make_clipping_tree(tmp_path)

    async def go():
        rows, width = await _rows_at(tmp_path, (110, 38))
        _assert_no_clipped_tail(rows, width)
        # Narrower panel, same guarantee: bars may shrink or vanish, but
        # whatever percent is shown is whole.
        tests_row = next((r for r in rows if "tests/" in r), None)
        assert tests_row is not None, f"no tests/ row rendered; rows={rows}"
        assert re.search(r"\s\d\d\.\d%$", tests_row), (
            f"tests/ row lost its full percent when narrow: {tests_row!r}"
        )

    asyncio.run(go())


def test_labels_refit_when_the_panel_is_resized(tmp_path):
    """Shrinking the terminal must re-fit labels already on screen."""
    _make_clipping_tree(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(160, 38)) as pilot:
            await _wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree", SizeTree)
            await pilot.pause(delay=0.2)
            await _expand_everything(tree, pilot)
            wide = _visible_rows(tree)
            _assert_no_clipped_tail(wide, tree.scrollable_content_region.width)

            await pilot.resize_terminal(104, 38)
            await pilot.pause(delay=0.2)
            narrow = _visible_rows(tree)
            _assert_no_clipped_tail(narrow, tree.scrollable_content_region.width)
            assert narrow != wide, "labels were not re-fitted after the resize"

    asyncio.run(go())
