"""The footer has to fit an 80-column terminal on every screen.

80 columns is the floor a terminal app gets to assume. Before the keymap
work every screen blew through it — Explorer needed 187 cells, Cleanup 158,
Monitor 153, FS Overview 94 — and because Textual drops whatever does not
fit off the end, and the app-level bindings are declared last, the two keys
every user needs first (`?` and `q`) were the two that were never on screen.

This gate **measures the real widget** rather than modelling it. Composing
the shown bindings by hand and adding up string lengths is what the original
investigation did, and it came out 2 cells low on all four screens: the
model missed the description padding on the trailing key. Textual's own
layout is the only thing that knows the answer, and it is right here in the
test harness already.

The measurement runs on a *wide* terminal on purpose. `virtual_size` is
clamped to the container when content fits, so measuring at 80 reports 80
for anything that overflows and anything that just fits alike. Summing the
children's outer widths at 200 columns gives the intrinsic width, which is
the number the gate is actually about.
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

# Wide enough that no screen's footer is clamped by its container.
_MEASURE_WIDTH = 200


def _tree(tmp_path: pathlib.Path) -> str:
    for index in range(6):
        (tmp_path / f"file_{index}.txt").write_text("x" * (100 * (index + 1)))
    return str(tmp_path)


async def _footer_width(pilot, app) -> int:
    """Intrinsic width of the active screen's footer, in cells."""
    footer = app.screen.query_one(Footer)
    return sum(child.outer_size.width for child in footer.children)


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

            width = await _footer_width(pilot, app)
            assert width <= MAX_FOOTER_COLUMNS, (
                f"{screen_name} footer needs {width} columns, "
                f"{width - MAX_FOOTER_COLUMNS} more than the {MAX_FOOTER_COLUMNS}"
                " a terminal is allowed to be. Give a pair of related bindings a "
                "shared `group=` (Textual renders the group's keys bare plus one "
                "label) or drop one to `show=False` — it stays in `?` and the "
                "command palette either way."
            )

    asyncio.run(go())


def test_quit_and_keymap_survive_a_narrow_terminal(tmp_path):
    """The two keys a new user needs first must be in the footer.

    This is the failure the gate exists to prevent, stated directly: these
    two are declared last, so they are the first to fall off the end.
    """
    scan_path = _tree(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=scan_path, show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(MAX_FOOTER_COLUMNS, 30)) as pilot:
            await wait_for_explorer(pilot, app)
            footer = app.screen.query_one(Footer)
            shown = {
                child.key
                for child in footer.children
                if getattr(child, "key", None)
            }
            assert "q" in shown, "quit fell off the footer"
            assert "question_mark" in shown, "the key map fell off the footer"

    asyncio.run(go())
