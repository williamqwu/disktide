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

The perception: the neutral directory ladder is where a theme's
temperature lives, the category hues being shared by design, and it used
to carry so little chroma that the whole chain could work perfectly and
still look like nothing had happened. `test_the_ladders_are_visibly_apart`
pins the gate that keeps the tables honest about that.

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
from disktide.viz.colors import NEUTRAL_DIR_RGB, get_color_scheme
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


def test_the_ladders_are_visibly_apart():
    """The tables themselves have to carry a difference worth seeing.

    Every other test here can pass on a palette nobody can tell apart:
    the epoch moves, the strips differ by a count or two, and the user
    still says the theme picker does nothing. This is the gate that
    stops that regressing -- at each of the three shallowest depths,
    which is nearly all of a sunburst's directory area, every pair of
    temperatures separates by at least 25 on some sRGB channel.
    """
    for depth in (0, 1, 2):
        ladders = {
            theme: NEUTRAL_DIR_RGB[theme][depth]
            for theme in ("default", "warm", "cold")
        }
        names = list(ladders)
        for i, first in enumerate(names):
            for second in names[i + 1:]:
                apart = max(
                    abs(a - b)
                    for a, b in zip(ladders[first], ladders[second])
                )
                assert apart >= 25, (
                    f"{first} and {second} neutrals at depth {depth} differ "
                    f"by only {apart} ({ladders[first]} vs {ladders[second]}"
                    f"); below a just-noticeable difference the theme picker "
                    f"reads as broken"
                )


def test_the_preview_row_shows_the_picked_theme_not_the_active_one():
    """`theme_preview` reads the named theme's tables directly.

    It has to: the row is redrawn from inside the change handler, and
    binding it to the active scheme would make it a lagging echo of the
    previous pick rather than a preview.
    """
    warm = _text_styles(theme_preview("warm"))
    cold = _text_styles(theme_preview("cold"))
    assert warm != cold
    assert any(_rgb(NEUTRAL_DIR_RGB["warm"][0]) in style for style in warm)
    assert any(_rgb(NEUTRAL_DIR_RGB["cold"][0]) in style for style in cold)


def test_picking_a_theme_repaints_the_disc_underneath(tmp_path):
    """The whole chain, from keystroke to a different mid-disc row."""
    root = _scan_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(root), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            _explorer, view = await _settled_explorer(pilot, app)
            # The default config is warm, and `App.on_mount` applies it.
            assert get_color_scheme().name == "warm"
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
                "it is still drawing from the layout it baked warm RGB into"
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
