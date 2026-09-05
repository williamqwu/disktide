"""The tree's bar+percent tail must fit the panel instead of being cropped.

Textual crops a Tree label at the panel edge, so a fixed-width bar plus
percent on a deeply-indented row used to render as a fragment ("16." /
"0.2" with the digits and sign shorn off). SizeTree now measures the room
left on each row and degrades the bar before the percent.
"""

from __future__ import annotations

import asyncio
import re

from rich.style import Style

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.viz.colors import ink_fill
from disktide.widgets.size_tree import SizeTree
from tests.waiting import wait_for_explorer

# A percent that survived the crop intact: digits, a decimal, one digit, "%".
COMPLETE_PCT = re.compile(r"\d+\.\d%")
# The classic clipped tails: "…16." and "…0.2" with nothing after them.
FRAGMENT = re.compile(r"\d+\.$|\d+\.\d$")


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
    checked = 0
    for row in rows:
        # The bar itself is spaces under a background colour now, so the
        # percent is the whole of the tail's plain text and the only thing
        # a crop can shear.
        if "%" not in row:
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
        await wait_for_explorer(pilot, app)
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
            await wait_for_explorer(pilot, app)
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


def _fill_colours(strip) -> set:
    """Background colours of the space runs in a rendered row.

    The bar is the only thing in a tree row drawn as spaces under a
    colour, so this is the bar's fill and its track -- `default` dropped,
    because that is the widget's own background showing through.
    """
    return {
        segment.style.bgcolor
        for segment in strip
        if segment.text.strip() == ""
        and segment.style is not None
        and segment.style.bgcolor is not None
        and not segment.style.bgcolor.is_default
    }


def test_the_bar_survives_the_cursor_row(tmp_path, monkeypatch):
    """A background bar under a background highlight, in both colour modes.

    `Tree.render_label` stylizes the whole label with the cursor style
    *after* the label's own spans, and a later Rich span's bgcolor wins.
    The old glyph bar was a foreground and never noticed; a bar drawn as a
    background is painted over by the highlight and disappears on exactly
    the row the user is looking at. `SizeTree.render_label` puts it back,
    and the check is that the selected row still carries the same two
    fills an unselected one does.

    Both colour modes, because the `ansi` scheme reaches this through a
    different set of colours and is the mode a 16-colour web shell gets.
    """
    _make_clipping_tree(tmp_path)

    async def go(ansi: bool):
        if ansi:
            monkeypatch.setenv("NO_COLOR", "1")
        else:
            monkeypatch.delenv("NO_COLOR", raising=False)
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(140, 38)) as pilot:
            await wait_for_explorer(pilot, app)
            assert bool(app.ansi_color) is ansi
            tree = app.screen.query_one("#size-tree", SizeTree)
            await pilot.pause(delay=0.2)
            await _expand_everything(tree, pilot)
            tree.focus()
            # Two adjacent directory rows: the cursor goes on the first and
            # the second is the control.
            rows = [
                line
                for line, node in enumerate(tree._tree_lines)
                if line > 0
                and node.node.data is not None
                and node.node.data.is_dir
            ]
            assert len(rows) >= 2, rows
            cursor, control = rows[0], rows[1]
            tree.cursor_line = cursor
            await pilot.pause()
            assert tree.cursor_line == cursor

            expected = {
                Style.parse(ink_fill(role)).bgcolor
                for role in ("bar", "bar_track")
            }
            assert len(expected) == 2, expected
            control_fills = _fill_colours(tree.render_line(control))
            assert expected <= control_fills, (
                "an unselected row is not drawing the bar at all: "
                f"{sorted(str(c) for c in control_fills)}"
            )
            on_cursor = _fill_colours(tree.render_line(cursor))
            assert expected <= on_cursor, (
                f"the cursor row lost the bar: {sorted(str(c) for c in on_cursor)}"
                f" does not cover {sorted(str(c) for c in expected)}"
            )

    asyncio.run(go(False))
    asyncio.run(go(True))
