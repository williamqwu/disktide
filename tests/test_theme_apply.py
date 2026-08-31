"""Picking a colour theme has to change the picture, immediately.

Two things had to be true for the Settings theme picker to feel like it
worked, and for a long time only the second one was.

The mechanism: `on_select_changed` calls `set_color_scheme`, which bumps
the render epoch; the swatch row redraws from the picked theme's own
tables; and dismissing Settings repaints the screen it reveals, whose
chart rebuilds the layout it had baked the old RGB into. Every link in
that chain is cached somewhere, so these drive it the way a user does --
keystrokes, a real scan, real rendered strips -- rather than calling the
setter and trusting the rest.

The perception: a theme change has to be *visible*. That used to rest on
the neutral directory ladder alone, the category hues being shared by
design, and the ladder carried so little chroma that the whole chain could
work perfectly and still look like nothing had happened. Themes now move
the categories and the application chrome too; how far apart the tables sit
is measured in `tests/test_palette_gates.py`, and what these tests add is
that the movement actually reaches the screen.

Deadline polls throughout, never a fixed sleep: CI runners are two-core
and the layout rebuild is deliberately deferred a frame past the paint
(see `SunburstView._ensure_layout`).
"""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console
from rich.text import Text
from textual.widgets import Select, Static

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.rendering import render_epoch
from disktide.screens.explorer import ExplorerScreen
from disktide.screens.settings import SettingsScreen, theme_preview
from disktide.viz.colors import NEUTRAL_DIR_RGB, SCHEMES, get_color_scheme
from disktide.widgets.sunburst_view import SunburstView

# How long a deadline poll waits before giving up: 40 pumps at 0.1s.
_POLL_TRIES = 40
_POLL_DELAY = 0.1


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never read or write the developer's real config.

    Dismissing Settings calls `save_config`, so a test that drove the
    picker without this would rewrite `~/.config/disktide/config.toml`
    with whatever theme it happened to pick last.
    """
    for var, leaf in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ):
        monkeypatch.setenv(var, str(tmp_path / "xdg" / leaf))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _scan_dir(tmp_path) -> None:
    """A tree with two clearly-dominated subdirectories, so the disc has
    both tinted directory arcs and file arcs to colour."""
    root = tmp_path / "tree"
    code = root / "code"
    code.mkdir(parents=True)
    (code / "app.py").write_text("x" * 4000)
    pics = root / "pics"
    pics.mkdir()
    (pics / "a.png").write_bytes(b"\x89PNG" + b"y" * 4000)
    return root


def _rgb(color: tuple[int, int, int]) -> str:
    return f"rgb({color[0]},{color[1]},{color[2]})"


def _styles(strip) -> list[str]:
    """A rendered row as the styles it paints.

    `Strip` is iterable but exposes no `.segments`, and the colours are
    what this test is about, so the text is dropped.
    """
    return [str(segment.style) for segment in strip]


def _text_styles(text: Text) -> list[str]:
    """The styles a Rich `Text` paints.

    Rendering spans needs a real Console to resolve styles against, so
    this hands it a throwaway one rather than None.
    """
    return [str(segment.style) for segment in text.render(Console())]


def _preview_styles(screen) -> list[str]:
    """The swatch row's styles, as mounted.

    `Static` exposes no `.renderable`; ask it to render. What comes back
    is a Textual `Content`, not the Rich `Text` that went in, and its
    `render` yields (text, style) pairs and takes no Console -- hence the
    second helper rather than reusing `_text_styles`.
    """
    return [str(style) for _text, style in screen.query_one(
        "#theme-preview", Static
    ).render().render(None)]


async def _settled_explorer(pilot, app):
    """Wait until the explorer holds a scan *and* a built sunburst layout.

    A layout is only built inside `render_line`, so this waits on the
    paint rather than on the scan finishing.
    """
    await pilot.pause(delay=0.2)
    for _ in range(_POLL_TRIES):
        await pilot.pause(delay=_POLL_DELAY)
        screen = app.screen
        if not isinstance(screen, ExplorerScreen) or screen._root is None:
            continue
        view = screen.query_one("#sunburst-view", SunburstView)
        if view._category_index is not None and view._layout is not None:
            return screen, view
    raise AssertionError("the explorer never painted a sunburst layout")


async def _await_screen(pilot, app, screen_class):
    for _ in range(_POLL_TRIES):
        await pilot.pause(delay=0.05)
        if isinstance(app.screen, screen_class):
            await pilot.pause()
            return app.screen
    raise AssertionError(
        f"never reached {screen_class.__name__}; still on "
        f"{app.screen.__class__.__name__}"
    )


async def _await_fresh_layout(pilot, view):
    """Wait out the deferred rebuild.

    `_ensure_layout` will not swap a layout in mid-paint: it draws one
    more frame from the stale one and queues the rebuild with
    `call_next`. So the epoch match is reached a frame or two after the
    dismissal, not on it.
    """
    for _ in range(_POLL_TRIES):
        if view._layout_epoch == render_epoch() and view._layout is not None:
            return
        view.refresh()
        await pilot.pause(delay=_POLL_DELAY)
    raise AssertionError(
        f"the sunburst never rebuilt under the current epoch "
        f"(layout {view._layout_epoch} vs render {render_epoch()})"
    )


async def _pick_theme(pilot, screen, theme: str) -> None:
    """Set the picker the way the dropdown does, and let the message land."""
    screen.query_one("#color-theme", Select).value = theme
    await pilot.pause()
    await pilot.pause()


def test_the_preview_row_shows_the_picked_theme_not_the_active_one():
    """`theme_preview` reads the named theme's tables directly.

    It has to: the row is redrawn from inside the change handler, and
    binding it to the active scheme would make it a lagging echo of the
    previous pick rather than a preview.
    """
    disktide = _text_styles(theme_preview("disktide"))
    cold = _text_styles(theme_preview("cold"))
    assert disktide != cold
    assert any(_rgb(NEUTRAL_DIR_RGB["disktide"][0]) in style for style in disktide)
    assert any(_rgb(NEUTRAL_DIR_RGB["cold"][0]) in style for style in cold)


def test_the_preview_row_steps_for_mono_too():
    """`mono` used to draw six identical swatches.

    Its six categories are a gray ladder now, and the swatch row is the
    only place a user sees all six side by side before committing to the
    theme -- if it renders one gray six times the picker is lying about
    what it is offering.
    """
    styles = _text_styles(theme_preview("mono"))
    assert len(set(styles)) >= 9, (
        f"mono's preview drew only {len(set(styles))} distinct styles; "
        f"three neutrals plus six stepped category grays were expected"
    )


def test_every_theme_previews_without_reaching_a_missing_table():
    """The picker offers five themes, so five have to draw.

    `mono` reaches a different swatch table from the other four and the
    preview is built before any of them is active, so a key that exists in
    `SCHEMES` but not in the generated tables would only surface here.
    """
    for name in SCHEMES:
        assert _text_styles(theme_preview(name)), name


def test_picking_a_theme_repaints_the_disc_underneath(tmp_path):
    """The whole chain, from keystroke to a different mid-disc row."""
    root = _scan_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(root), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            _explorer, view = await _settled_explorer(pilot, app)
            # The default config is disktide, and `App.on_mount` applies it.
            assert get_color_scheme().name == "disktide"
            assert app.theme == "textual-dark", (
                "disktide must stay on the built-in Textual theme; a copy "
                "under another name would put the README shots at risk"
            )
            before = _styles(view.render_line(view.size.height // 2))
            assert before, "the mid-disc row painted nothing to compare"

            await pilot.press("question_mark")
            settings = await _await_screen(pilot, app, SettingsScreen)
            preview_before = _preview_styles(settings)

            await _pick_theme(pilot, settings, "cold")

            assert get_color_scheme().name == "cold", (
                "the picker did not apply the scheme"
            )
            preview_after = _preview_styles(settings)
            assert preview_after != preview_before, (
                "the swatch row did not redraw for the new theme"
            )
            cold_neutral = _rgb(NEUTRAL_DIR_RGB["cold"][0])
            assert any(cold_neutral in style for style in preview_after), (
                f"the swatch row does not show the cold neutral "
                f"{cold_neutral}; it is not reflecting the pick"
            )

            await pilot.press("escape")
            await _await_screen(pilot, app, ExplorerScreen)
            await _await_fresh_layout(pilot, view)

            after = _styles(view.render_line(view.size.height // 2))
            assert after != before, (
                "the sunburst repainted the same row after a theme change; "
                "it is still drawing from the layout it baked the old "
                "scheme's RGB into"
            )
            assert app.theme == "disktide-cold", (
                "the chart repainted but the application chrome did not "
                "follow; picking a theme has to move both halves"
            )

    asyncio.run(go())


def test_a_second_visit_applies_a_theme_just_as_well(tmp_path):
    """Settings is installed once and re-pushed, so entry state is reused.

    `on_mount` runs only on the first visit; anything it records about
    the render epoch is stale by the second, which is why
    `on_screen_resume` re-reads it. A second round trip through the
    screen has to change the disc exactly as the first one did.
    """
    root = _scan_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(root), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            _explorer, view = await _settled_explorer(pilot, app)

            await pilot.press("question_mark")
            settings = await _await_screen(pilot, app, SettingsScreen)
            await _pick_theme(pilot, settings, "cold")
            await pilot.press("escape")
            await _await_screen(pilot, app, ExplorerScreen)
            await _await_fresh_layout(pilot, view)
            cold_row = _styles(view.render_line(view.size.height // 2))

            # Second visit: same installed screen, resumed rather than mounted.
            await pilot.press("question_mark")
            settings_again = await _await_screen(pilot, app, SettingsScreen)
            assert settings_again is settings, (
                "the settings screen was rebuilt; this test is meant to "
                "cover the installed-screen re-entry path"
            )
            assert settings_again._entry_epoch == render_epoch(), (
                "on_screen_resume did not re-read the render epoch, so a "
                "dismissal that changed nothing would still repaint"
            )

            await _pick_theme(pilot, settings_again, "mono")
            assert get_color_scheme().name == "mono"

            await pilot.press("escape")
            await _await_screen(pilot, app, ExplorerScreen)
            await _await_fresh_layout(pilot, view)

            mono_row = _styles(view.render_line(view.size.height // 2))
            assert mono_row != cold_row, (
                "the second theme change did not reach the disc"
            )

    asyncio.run(go())


def test_dismissing_without_a_change_leaves_the_epoch_alone(tmp_path):
    """The repaint on dismissal is conditional, and has to stay that way.

    `action_dismiss_settings` only redraws the revealed screen when the
    epoch moved while Settings was open. With `_entry_epoch` recorded
    once at mount, a later visit compared against a long-stale number and
    repainted every time.
    """
    root = _scan_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(root), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            _explorer, view = await _settled_explorer(pilot, app)

            # First visit changes the theme, which moves the epoch.
            await pilot.press("question_mark")
            settings = await _await_screen(pilot, app, SettingsScreen)
            await _pick_theme(pilot, settings, "cold")
            await pilot.press("escape")
            await _await_screen(pilot, app, ExplorerScreen)
            await _await_fresh_layout(pilot, view)

            # Second visit touches nothing.
            await pilot.press("question_mark")
            await _await_screen(pilot, app, SettingsScreen)
            settled = render_epoch()
            assert settings._entry_epoch == settled

            await pilot.press("escape")
            await _await_screen(pilot, app, ExplorerScreen)
            await pilot.pause()

            assert render_epoch() == settled, (
                "an unchanged visit to Settings moved the render epoch"
            )
            assert view._layout_epoch == settled, (
                "an unchanged visit to Settings threw away a good layout"
            )

    asyncio.run(go())


def test_the_picker_seats_every_theme_name_on_one_line(tmp_path):
    """A wrapped theme name costs more than the line it takes.

    `Select` is `height: auto`, so a name too wide for the control grows
    it to four rows inside a three-row `.setting-row`: the swatch row
    beside it loses its baseline and the next row's control lands on top
    of this one. `Colorblind-safe` is the longest name the picker has to
    seat, and a setting row's three columns share only the 70 cells an
    80-column terminal leaves them -- so this drives the picker at the
    width where the fit is tightest rather than at whatever width a
    developer's terminal happens to be, and it checks the dropdown as
    well as the closed control, which are sized by different chrome.
    """
    root = _scan_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(root), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            settings = await _await_screen(pilot, app, SettingsScreen)
            picker = settings.query_one("#color-theme", Select)
            current = settings.query_one("#color-theme SelectCurrent #label", Static)

            for name, scheme in SCHEMES.items():
                await _pick_theme(pilot, settings, name)
                assert current.region.height == 1, (
                    f"{scheme.label!r} wrapped onto {current.region.height} "
                    f"lines: the closed picker leaves it "
                    f"{current.region.width} cells and it needs "
                    f"{len(scheme.label)}"
                )
                assert picker.region.height == 3, (
                    f"the picker grew to {picker.region.height} rows on "
                    f"{scheme.label!r}; it has three to live in"
                )

            assert settings.query_one("#theme-preview", Static).region.width == 20, (
                "the swatch row no longer has the 20 cells `theme_preview` "
                "draws into; widening the picker took them"
            )

            picker.expanded = True
            await pilot.pause()
            await pilot.pause()
            overlay = settings.query_one("#color-theme SelectOverlay")
            assert overlay.region.height == len(SCHEMES) + 2, (
                f"the open dropdown is {overlay.region.height} rows tall "
                f"for {len(SCHEMES)} themes plus its border; a name is "
                f"wrapping onto a second line in the list"
            )

    asyncio.run(go())
