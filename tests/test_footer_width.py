"""The footer has to fit an 80-column terminal on every screen.

80 columns is the floor a terminal app gets to assume. Before the keymap
work every screen blew through it — Explorer needed 187 cells, Cleanup 158,
Monitor 153, FS Overview 94 — and because Textual drops whatever does not
fit off the end, and the app-level bindings are declared last, the two keys
every user needs first (`?` and `q`) were the two that were never on screen.

**This gate reads the painted row.** Two earlier versions of it modelled the
footer instead and both were wrong in the safe-looking direction. Composing
the shown bindings by hand and adding up string lengths came out 2 cells low
on all four screens (it missed the description padding on the trailing key).
Summing `child.outer_size.width` came out 5 cells low on Cleanup — 75 where
the screen really paints 80 — because `outer_size` excludes margin and
`FooterKey.-grouped` carries `margin: 0 1`. A gate with five cells of phantom
headroom is worse than no gate: it passes the change that clips a key.

What the composited strip says has been checked against `tmux capture-pane`
at 80 columns and agrees cell for cell, so it is used here as ground truth.
The check is not a width comparison at all: it renders wide, where nothing
can be clipped, and then renders at 80 and asserts the same ink is still
there. That is the failure stated directly, and it needs no constant.

The measured widths are reported in the failure message because the number
is what tells you how much room a new key has: at the time of writing
Explorer paints 73, FS Overview 74, Cleanup 76 and Monitor 80 of 80.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest
from textual.widgets import Footer

from disktide.app import DiskTideApp
from disktide.config import load_config
from tests.waiting import wait_for_explorer

# The floor this gate defends. Raising it is a product decision, not a
# convenience: at 81 the footer starts losing keys off the right-hand end
# again, silently, on the narrowest terminal anyone still uses.
MAX_FOOTER_COLUMNS = 80

# Wide enough that no screen's footer is clamped by its container, so the
# strip rendered here is the whole footer and nothing has been dropped.
_MEASURE_WIDTH = 200


def _tree(tmp_path: pathlib.Path) -> str:
    for index in range(6):
        (tmp_path / f"file_{index}.txt").write_text("x" * (100 * (index + 1)))
    return str(tmp_path)


def _footer_runs(app) -> tuple[str, str]:
    """The footer's two painted runs: the keys, and the docked palette key.

    They are returned separately because the gap between them is whatever
    the terminal width leaves over, and that is the one part of the row
    that is *supposed* to change when the terminal narrows.
    """
    footer = app.screen.query_one(Footer)
    # The composited strip, not the widget's own render: a widget renders
    # its own content and the compositor is what puts the children on the
    # row. `tests/test_theme_apply.py` reaches for it the same way.
    text = app.screen._compositor.render_strips()[footer.region.y].text
    docked = [c for c in footer.children if c.styles.dock == "right"]
    split = min((c.region.x for c in docked), default=len(text))
    return text[:split].rstrip(), text[split:].rstrip()


def _painted_width(app) -> int:
    """Cells the footer needs before the terminal starts eating it."""
    keys, palette = _footer_runs(app)
    return len(keys) + len(palette)


@pytest.mark.parametrize(
    ("mode_key", "screen_name"),
    [
        (None, "ExplorerScreen"),
        ("2", "MonitorScreen"),
        ("3", "FSOverviewScreen"),
        ("4", "CleanupScreen"),
    ],
)
def test_footer_fits_eighty_columns(tmp_path, mode_key, screen_name):
    scan_path = _tree(tmp_path)

    async def go():
        config = load_config()
        # Cleanup mode is opt-in; the gate has to see its footer regardless.
        config.ui.show_cleanup = True
        app = DiskTideApp(
            scan_path=scan_path, show_welcome=False, config=config
        )
        async with app.run_test(size=(_MEASURE_WIDTH, 30)) as pilot:
            await wait_for_explorer(pilot, app)
            if mode_key is not None:
                await pilot.press(mode_key)
                await pilot.pause()
                await asyncio.sleep(0.3)
                await pilot.pause()

            assert type(app.screen).__name__ == screen_name, (
                f"expected to be on {screen_name}, got "
                f"{type(app.screen).__name__} — the mode key may have moved"
            )

            wide_keys, wide_palette = _footer_runs(app)
            width = len(wide_keys) + len(wide_palette)

            await pilot.resize_terminal(MAX_FOOTER_COLUMNS, 30)
            await pilot.pause()
            narrow_keys, narrow_palette = _footer_runs(app)

            assert (narrow_keys, narrow_palette) == (wide_keys, wide_palette), (
                f"{screen_name}'s footer needs {width} columns and loses ink "
                f"at {MAX_FOOTER_COLUMNS}.\n"
                f"  wide:   {wide_keys!r} … {wide_palette!r}\n"
                f"  at {MAX_FOOTER_COLUMNS}: {narrow_keys!r} … {narrow_palette!r}\n"
                "Give a pair of related bindings a shared `group=` (Textual "
                "renders the group's keys bare plus one label) or drop one to "
                "`show=False` — it stays in `?` and the command palette either "
                "way."
            )
            assert width <= MAX_FOOTER_COLUMNS, (
                f"{screen_name} footer paints {width} columns, "
                f"{width - MAX_FOOTER_COLUMNS} more than the "
                f"{MAX_FOOTER_COLUMNS} a terminal is allowed to be."
            )

    asyncio.run(go())


def test_a_compact_group_is_only_for_the_mode_digits(tmp_path):
    """No footer group may print its keys with nothing between them.

    `compact=True` is how `1234 Mode` is drawn, and it reads as a range
    because digits do. Letters do not: the same setting shipped `ui Nav`,
    `spacea Select`, `pz Plan` and `bv Diff`, four tokens that each name a
    chord no terminal can send. The digits are the exception and they are
    the only one.
    """
    from disktide import keys as keys_module
    from textual.binding import Binding

    compact = {
        name: value
        for name, value in vars(keys_module).items()
        if isinstance(value, Binding.Group) and value.compact
    }
    assert set(compact) == {"MODE"}, (
        "these footer groups run their keys together, which reads as a key "
        f"combination: {sorted(set(compact) - {'MODE'})}"
    )


def test_quit_and_keymap_survive_a_narrow_terminal(tmp_path):
    """The two keys a new user needs first must be in the footer.

    This is the failure the gate exists to prevent, stated directly: these
    two are declared last, so they are the first to fall off the end. The
    assertion is on the painted row, because a `FooterKey` whose ink was
    overwritten by the docked palette key is still a child of the footer.
    """
    scan_path = _tree(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=scan_path, show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(MAX_FOOTER_COLUMNS, 30)) as pilot:
            await wait_for_explorer(pilot, app)
            keys, _palette = _footer_runs(app)
            assert "q Quit" in keys, f"quit fell off the footer: {keys!r}"
            assert "? Keys" in keys, f"the key map fell off the footer: {keys!r}"

    asyncio.run(go())
