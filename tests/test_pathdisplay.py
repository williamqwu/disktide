"""A path that does not fit must lose its prefix, never its tail.

Every place DiskTide shows a path in a fixed number of cells used to crop
it on the right, which on an HPC filesystem keeps `/fs/project/PRJ0042` -- the
four segments every path on the machine shares -- and drops the directory
the row was about. The breadcrumb, the Details panel, the growth heatmap
and the monitor's history table all read from the two helpers here.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult

from disktide.glyphs import visible_width
from disktide.pathdisplay import elide_path, elide_text, relative_label
from disktide.rendering import set_safe_rendering
from disktide.widgets.breadcrumb import Breadcrumb


LONG = "/fs/project/PRJ0042/alice/coreutil/disktide/scratchpad/stage/home/datasets/raw"


class TestElidePath:
    def test_a_path_that_fits_is_untouched(self):
        assert elide_path("/srv/data", 40) == "/srv/data"

    def test_no_measurement_yet_returns_the_path(self):
        # Before the first layout a widget has width 0; it re-fits later.
        assert elide_path(LONG, 0) == LONG

    def test_the_tail_survives_and_the_head_goes(self):
        fitted = elide_path(LONG, 24)
        assert visible_width(fitted) <= 24
        assert fitted.endswith("raw")
        assert fitted.startswith("…")
        assert "PRJ0042" not in fitted

    def test_as_much_of_the_tail_as_fits_is_kept(self):
        assert visible_width(elide_path(LONG, 40)) <= 40
        # A wider budget keeps strictly more of the path.
        assert len(elide_path(LONG, 40)) > len(elide_path(LONG, 24))

    def test_the_last_segment_is_cropped_only_as_a_last_resort(self):
        fitted = elide_path(LONG, 5)
        assert visible_width(fitted) <= 5
        assert fitted.endswith("raw")

    def test_absurdly_narrow_still_fits(self):
        for width in (1, 2, 3):
            assert visible_width(elide_path(LONG, width)) <= width

    def test_safe_rendering_uses_ascii(self):
        set_safe_rendering(True)
        try:
            assert elide_path(LONG, 24).startswith("...")
        finally:
            set_safe_rendering(False)


class TestRelativeLabel:
    def test_the_root_itself_is_a_dot(self):
        assert relative_label("/srv/data", "/srv/data") == "."
        assert relative_label("/srv/data/", "/srv/data") == "."

    def test_a_child_loses_the_shared_prefix(self):
        assert relative_label("/srv/data/logs", "/srv/data") == "logs"

    def test_a_sibling_with_a_shared_prefix_is_not_mangled(self):
        # "/srv/database" starts with "/srv/data" as a *string* but is not
        # under it as a path.
        assert relative_label("/srv/database", "/srv/data") == "/srv/database"

    def test_no_root_leaves_the_path_alone(self):
        assert relative_label("/srv/data", None) == "/srv/data"


class TestElideText:
    def test_prose_is_cropped_on_the_right(self):
        assert elide_text("hello world", 8) == "hello w…"

    def test_short_prose_is_untouched(self):
        assert elide_text("hi", 8) == "hi"


class _BreadcrumbApp(App):
    def compose(self) -> ComposeResult:
        yield Breadcrumb(LONG, id="bc")


def _breadcrumb_at(width: int) -> str:
    plain: list[str] = []

    async def go() -> None:
        app = _BreadcrumbApp()
        async with app.run_test(size=(width, 6)) as pilot:
            crumb = app.query_one("#bc", Breadcrumb)
            crumb.update_path(LONG, access="partial")
            await pilot.pause()
            plain.append(crumb.render().plain)

    asyncio.run(go())
    return plain[0]


class TestBreadcrumbFolding:
    def test_a_wide_terminal_shows_the_whole_trail(self):
        rendered = _breadcrumb_at(160)
        assert "PRJ0042" in rendered
        assert rendered.rstrip().endswith("◐︎") or "raw" in rendered

    def test_a_narrow_terminal_keeps_the_current_directory(self):
        for width in (100, 60, 30, 16):
            rendered = _breadcrumb_at(width)
            # The directory the user is standing in is the one crumb that
            # must never be the one dropped.
            assert "raw" in rendered, (width, rendered)
            assert visible_width(rendered) <= width

    def test_the_fold_is_marked(self):
        assert "…" in _breadcrumb_at(30)
