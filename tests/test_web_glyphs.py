"""Nothing DiskTide draws may be a block element.

The failure this gates is invisible in a real terminal and in `capture-pane`,
because both draw every block element at one cell. xterm.js does not. Its
default face is Courier New, which has no glyph at all for the eighth blocks
or the quadrants -- the browser falls back to a proportional font and draws
them at *that* font's advance, which is how a 72-cell Input came to draw its
`▔` row 129 cells wide and its `▁` row 86 -- and whose glyphs for the block
elements it *does* carry are not fitted to a terminal cell either: `░` comes
out 1.1 cells wide and 1.4 rows tall, `▀`/`▄` narrower than the cell. The
first reading of this policy allowed the second group. It should not have,
and the range is the rule now.

So the gate cannot be a screenshot comparison; there is no host here that
renders the way the browser does. It is a check on what the app *asks for*:
which border styles the widgets resolve to, and which glyphs
`ScrollBarRender` puts in a thumb end. Both are host-independent, and both
are where the unsafe glyphs came from -- the app's own stylesheet never
named one. `tool/capture_glyphs.py` is the other half, reading real panes.
"""

from __future__ import annotations

import asyncio

import pytest
from textual._border import BORDER_CHARS
from textual.scrollbar import ScrollBarRender
from textual.widgets import Button, Checkbox, Input, Select, Switch

from disktide.app import DiskTideApp
from disktide.config import AppConfig
from disktide.glyphs import (
    SAFE_BORDER_STYLES,
    SCROLLBAR_HORIZONTAL_BARS,
    SCROLLBAR_VERTICAL_BARS,
    UNSAFE_BORDER_STYLES,
    UNSAFE_GLYPHS,
    WEB_SAFE_GLYPHS,
    unsafe_glyphs_in,
    use_web_safe_scrollbars,
)
from disktide.screens.explorer import ExplorerScreen
from tests.waiting import wait_for_explorer, wait_until

BORDER_EDGES = ("border_top", "border_right", "border_bottom", "border_left")


def _border_glyphs(style: str) -> str:
    return "".join("".join(row) for row in BORDER_CHARS[style])


def test_the_unsafe_set_is_the_whole_block_elements_range():
    """No exceptions inside U+2580-U+259F, and none leaking into the allowlist.

    The first version of this policy carved out the eight block elements
    WGL4 carries, on the argument that a font either has a glyph or it does
    not. Courier New has them and draws them at the wrong size, so having
    the glyph was never the question.
    """
    assert UNSAFE_GLYPHS == {chr(code) for code in range(0x2580, 0x25A0)}
    assert not WEB_SAFE_GLYPHS & UNSAFE_GLYPHS
    # The box-drawing block right below it is untouched: it was verified on
    # the same capture and is what the borders are drawn from now.
    assert {chr(code) for code in range(0x2500, 0x2580)} <= WEB_SAFE_GLYPHS


def test_the_two_border_style_sets_cover_textual_and_nothing_else():
    """A new Textual release must not add a style neither set knows."""
    assert SAFE_BORDER_STYLES | UNSAFE_BORDER_STYLES == set(BORDER_CHARS)
    assert not (SAFE_BORDER_STYLES & UNSAFE_BORDER_STYLES)


def test_no_safe_border_style_draws_a_mis_measured_glyph():
    """The reason the safe list is the safe list."""
    offenders = {
        style: sorted(unsafe_glyphs_in(_border_glyphs(style)))
        for style in SAFE_BORDER_STYLES
        if unsafe_glyphs_in(_border_glyphs(style))
    }
    assert not offenders, offenders


def test_every_block_element_border_style_is_on_the_unsafe_list():
    """And the unsafe list is not merely a hand-written guess.

    `round`, `dashed` and `heavy` are on it for a second reason -- their
    glyphs render at one cell today but sit outside the CP437 set the rest
    of this policy is drawn from -- so they are the allowed slack, and
    nothing else is.
    """
    by_glyph = {
        style for style in BORDER_CHARS if unsafe_glyphs_in(_border_glyphs(style))
    }
    assert by_glyph <= UNSAFE_BORDER_STYLES
    assert UNSAFE_BORDER_STYLES - by_glyph == {"round", "dashed", "heavy"}


def test_the_scrollbar_lists_are_installed_by_building_an_app():
    """One call at construction, so every entry point inherits it."""
    ScrollBarRender.VERTICAL_BARS = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", " "]
    ScrollBarRender.HORIZONTAL_BARS = ["▉", "▊", "▋", "▌", "▍", "▎", "▏", " "]
    try:
        DiskTideApp(config=AppConfig())
        assert ScrollBarRender.VERTICAL_BARS == SCROLLBAR_VERTICAL_BARS
        assert ScrollBarRender.HORIZONTAL_BARS == SCROLLBAR_HORIZONTAL_BARS
    finally:
        use_web_safe_scrollbars()


@pytest.mark.parametrize("vertical", [True, False])
def test_a_rendered_scrollbar_thumb_end_is_never_a_mis_measured_glyph(vertical):
    """Every sub-cell position of the thumb, at both ends.

    `render_bar` reads `bars[len(bars) - 1 - bar]` for the head and the
    tail, so a list with a mis-measured glyph anywhere in it shows up only
    at the scroll positions that land on that index -- which is why this
    sweeps positions rather than checking one.
    """
    use_web_safe_scrollbars()
    seen: set[str] = set()
    for position in range(0, 400):
        segments = ScrollBarRender.render_bar(
            size=25,
            virtual_size=400,
            window_size=25,
            position=position * 0.94,
            vertical=vertical,
        )
        for segment in segments.segments:
            seen.update(segment.text)
    assert not seen & UNSAFE_GLYPHS, sorted(seen & UNSAFE_GLYPHS)
    # And nothing else either: with both list entries a space, `render_bar`
    # skips the partial-cell segment entirely and the thumb is whole cells
    # of background colour, so a space is the only glyph a scrollbar emits.
    assert seen - {"\n"} == {" "}, sorted(seen)
    assert set(SCROLLBAR_VERTICAL_BARS + SCROLLBAR_HORIZONTAL_BARS) == {" "}


def _unsafe_borders(screen, label: str) -> list[str]:
    """Every widget on *screen* whose resolved border is unsafe."""
    offenders = []
    for widget in [screen, *screen.walk_children(with_self=False)]:
        for edge in BORDER_EDGES:
            edge_style = getattr(widget.styles, edge, None)
            if edge_style is None:
                continue
            name = edge_style[0]
            if name in UNSAFE_BORDER_STYLES:
                offenders.append(
                    f"{label}: {type(widget).__name__}"
                    f"(id={widget.id}, classes={sorted(widget.classes)}) "
                    f"{edge.replace('_', '-')}: {name}"
                )
    return offenders


def _tree(tmp_path):
    for index in range(6):
        directory = tmp_path / f"d{index}"
        directory.mkdir()
        (directory / "big.bin").write_bytes(b"x" * (4096 * (index + 1)))
    cache = tmp_path / "d0" / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_bytes(b"cache")


@pytest.mark.parametrize("ansi", [False, True], ids=["rgb", "ansi"])
def test_no_screen_asks_for_a_border_the_browser_cannot_draw(
    tmp_path, monkeypatch, ansi
):
    """Welcome, explorer, settings, the key map, a modal, and a toast.

    Both colour modes, because Textual keeps a second copy of half its
    chrome behind `:ansi` -- and `:ansi` is exactly the mode a 16-colour
    web shell gets, which is where the tearing was reported from.
    """
    _tree(tmp_path)
    if ansi:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    async def go():
        from disktide.widgets.confirm_modal import ConfirmModal

        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=True, config=AppConfig()
        )
        offenders: list[str] = []
        async with app.run_test(size=(160, 50)) as pilot:
            await wait_until(
                pilot,
                lambda: bool(app.screen.query("#path-input")),
                what="the welcome screen never composed its path input",
            )
            # Proof both branches were actually exercised.
            assert bool(app.ansi_color) is ansi
            offenders += _unsafe_borders(app.screen, "welcome")
            # Focused and invalid states resolve to their own rules.
            path_input = app.screen.query_one("#path-input", Input)
            path_input.add_class("-invalid")
            path_input.focus()
            await pilot.pause()
            offenders += _unsafe_borders(app.screen, "welcome (focus/invalid)")
            path_input.remove_class("-invalid")

            await pilot.press("enter")
            screen = await wait_for_explorer(pilot, app)
            offenders += _unsafe_borders(screen, "explorer")

            app.push_screen("settings")
            await pilot.pause()
            await wait_until(
                pilot,
                lambda: bool(app.screen.query(Select)),
                what="the settings screen never composed its selects",
            )
            offenders += _unsafe_borders(app.screen, "settings")
            # An open Select overlay is a separate widget with its own rule.
            app.screen.query(Select).first().expanded = True
            await pilot.pause()
            offenders += _unsafe_borders(app.screen, "settings (select open)")
            app.pop_screen()
            await pilot.pause()

            app.action_show_keymap()
            await pilot.pause()
            await pilot.pause()
            offenders += _unsafe_borders(app.screen, "key map")
            await pilot.press("escape")
            await pilot.pause()

            app.push_screen(ConfirmModal("Delete?", "Really?"))
            await pilot.pause()
            offenders += _unsafe_borders(app.screen, "confirm modal")
            await pilot.press("escape")
            await pilot.pause()

            app.notify("hello", severity="warning")
            await pilot.pause()
            await pilot.pause()
            offenders += _unsafe_borders(app.screen, "toast")

            await pilot.press("ctrl+p")
            await pilot.pause()
            await pilot.pause()
            offenders += _unsafe_borders(app.screen, "command palette")
            await pilot.press("escape")
            await pilot.pause()

        assert not offenders, "\n".join(offenders)

    asyncio.run(go())


def test_the_cleanup_modal_asks_for_nothing_unsafe_either(tmp_path):
    """It is a modal on top of the app, so it is walked on its own."""
    from disktide.cleanup.detector import detect_targets
    from disktide.repositories.sqlite import SQLiteSnapshotRepository
    from disktide.scanner.walker import scan_directory
    from disktide.services.cleanup import CleanupService
    from disktide.widgets.cleanup_modal import CleanupModal

    _tree(tmp_path)
    repository = SQLiteSnapshotRepository(str(tmp_path / "cleanup.db"))
    repository.connect()
    service = CleanupService(repository)
    plan = service.create_plan(
        tmp_path,
        detect_targets(scan_directory(str(tmp_path))),
        provenance="test",
    )

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=AppConfig()
        )
        async with app.run_test(size=(160, 50)) as pilot:
            await wait_for_explorer(pilot, app)
            app.push_screen(CleanupModal(plan))
            await pilot.pause()
            await pilot.pause()
            assert app.screen.query(Button)
            assert not _unsafe_borders(app.screen, "cleanup modal")

    try:
        asyncio.run(go())
    finally:
        repository.close()


def test_the_gate_would_notice_a_stock_widget_slipping_through(tmp_path):
    """The check fails when the override is missing, which is the point.

    A Checkbox and a Switch mounted with the app's stylesheet suppressed
    fall back to Textual's `tall`, and `_unsafe_borders` says so. Without
    this, a rule silently dropped from `App.CSS` would look like a pass.
    """

    async def go():
        class Bare(DiskTideApp):
            CSS = ""

        app = Bare(scan_path=str(tmp_path), show_welcome=False, config=AppConfig())
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_for_explorer(pilot, app)
            await app.screen.mount(Checkbox("x"), Switch())
            await pilot.pause()
            offenders = _unsafe_borders(app.screen, "bare")
            assert any("tall" in line for line in offenders), offenders
            assert isinstance(app.screen, ExplorerScreen)

    asyncio.run(go())


@pytest.mark.parametrize("ansi", [False, True], ids=["rgb", "ansi"])
def test_the_scan_overlay_draws_its_bar_as_a_fill(tmp_path, monkeypatch, ansi):
    """Textual's `Bar` renders `━`, with `╺`/`╸` for its two half-cell ends.

    All three are in the box-drawing block, so the allowlist alone would
    pass them -- and `━` really is fine, it is what the tab underline has
    always drawn with. The half-heavy ends are not: they are a partial
    cell, which is the shape of thing a browser terminal draws at the
    wrong width, on a row whose length changes as the bar advances. So the
    check is not "no forbidden glyph" but "no glyph at all": the bar is
    spaces under a background, and the progress is the boundary between
    two of them.

    Swept across the bar's three states, because `Bar.render` takes a
    different branch for each: indeterminate (what a scan actually shows),
    part-way, and complete.
    """
    from textual.widgets._progress_bar import Bar

    from disktide.widgets.scan_progress import ScanProgressOverlay

    if ansi:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=AppConfig()
        )
        async with app.run_test(size=(120, 32)) as pilot:
            await wait_for_explorer(pilot, app)
            assert bool(app.ansi_color) is ansi
            overlay = ScanProgressOverlay()
            await app.screen.mount(overlay)
            overlay.start(run_id="r", phase="walk", policy="default")
            await pilot.pause()
            progress_bar = overlay.query_one("#scan-bar")
            bar = progress_bar.query_one(Bar)
            for total, done in ((None, 0), (100, 30), (100, 100)):
                progress_bar.update(total=total, progress=done)
                await pilot.pause()
                await pilot.pause()
                strip = bar.render_line(0)
                assert not unsafe_glyphs_in(strip.text)
                assert set(strip.text) == {" "}, (
                    f"{total}/{done}: the bar drew {sorted(set(strip.text))}"
                )
                fills = {
                    segment.style.bgcolor
                    for segment in strip
                    if segment.style is not None
                    and segment.style.bgcolor is not None
                    and not segment.style.bgcolor.is_default
                }
                assert fills, f"{total}/{done}: the bar is not painted at all"
                if total is not None and 0 < done < total:
                    assert len(fills) == 2, (
                        f"{done}%: a part-way bar needs a done colour and a "
                        f"track colour, got {sorted(str(c) for c in fills)}"
                    )
            # And the whole overlay stays inside the reviewed set.
            for y in range(overlay.size.height):
                text = overlay.render_line(y).text
                assert not unsafe_glyphs_in(text), sorted(unsafe_glyphs_in(text))
                unknown = {
                    char
                    for char in text
                    if char not in WEB_SAFE_GLYPHS and not char.isspace()
                }
                assert not unknown, sorted(unknown)

    asyncio.run(go())
