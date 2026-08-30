"""The dominant-content rollup and the colours it drives.

Directories are most of a sunburst's area.  Left neutral they say nothing
about what they hold, so the chart's claim — that a fat subtree reads as
"all code" or "all media" at a glance — was only ever delivered on the
outer ring of file slivers.  These tests pin the rollup that fixes that,
the tint it feeds, and the legend that names the shares.
"""

from __future__ import annotations

import asyncio

import pytest

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.models.tree import FSNode
from disktide.screens.explorer import ExplorerScreen
from disktide.viz.categories import build_category_index
from disktide.viz.colors import (
    category_dir_tint,
    category_file_color,
    neutral_dir_color,
    set_color_scheme,
)
from disktide.viz.sunburst import _arc_color, _parse_rgb, compute_sunburst
from disktide.viz.treemap import _rect_bg, compute_layout
from disktide.widgets.sunburst_view import SunburstView
from disktide.widgets.treemap_view import TreemapView

PANEL_BG = (30, 30, 30)


@pytest.fixture(autouse=True)
def _use_default_scheme():
    set_color_scheme("default")
    yield
    set_color_scheme("default")


def _file(name: str, size: int, parent: str, depth: int) -> FSNode:
    return FSNode(
        name=name, path=f"{parent}/{name}", size=size, own_size=size,
        is_dir=False, depth=depth, file_count=1,
    )


def _dir(name: str, parent: str, depth: int, children: list[FSNode]) -> FSNode:
    return FSNode(
        name=name, path=f"{parent}/{name}",
        size=sum(c.size for c in children), own_size=0,
        is_dir=True, depth=depth, children=children,
    )


def _sample_tree() -> FSNode:
    """A root whose subtrees are each dominated by a different thing."""
    src = _dir("src", "/r", 1, [
        _file("app.py", 800, "/r/src", 2),
        _file("notes.md", 200, "/r/src", 2),
    ])
    # 500 bytes of media against 500 of data: an exact tie.
    tie = _dir("tie", "/r", 1, [
        _file("clip.mp4", 500, "/r/tie", 2),
        _file("train.parquet", 500, "/r/tie", 2),
    ])
    junk = _dir("junk", "/r", 1, [
        _file("Makefile", 900, "/r/junk", 2),
        _file("build.log", 100, "/r/junk", 2),
    ])
    empty = _dir("empty", "/r", 1, [])
    # A directory that owns no files directly: its shares come from below.
    nested = _dir("nested", "/r", 1, [
        _dir("inner", "/r/nested", 2, [
            _file("weights.safetensors", 4000, "/r/nested/inner", 3),
        ]),
    ])
    return _dir("r", "", 0, [src, tie, junk, empty, nested])


class TestCategoryIndex:
    def test_dominant_reports_category_and_share(self):
        index = build_category_index(_sample_tree())
        assert index.dominant("/r/src") == ("code", pytest.approx(0.8))

    def test_bytes_roll_up_through_intermediate_dirs(self):
        index = build_category_index(_sample_tree())
        assert index.dominant("/r/nested") == ("data", 1.0)
        assert index.dominant("/r/nested/inner") == ("data", 1.0)

    def test_root_mixes_every_subtree(self):
        index = build_category_index(_sample_tree())
        shares = dict(index.shares("/r"))
        # 4000 of the tree's 7000 bytes are the checkpoint under nested/inner.
        assert shares["data"] == pytest.approx(4500 / 7000)
        assert shares["code"] == pytest.approx(800 / 7000)
        assert "docs" in shares and "media" in shares and "other" in shares

    def test_shares_are_sorted_largest_first(self):
        index = build_category_index(_sample_tree())
        values = [share for _cat, share in index.shares("/r")]
        assert values == sorted(values, reverse=True)
        assert sum(values) == pytest.approx(1.0)

    def test_a_tie_resolves_deterministically(self):
        tree = _sample_tree()
        first = build_category_index(tree).dominant("/r/tie")
        second = build_category_index(tree).dominant("/r/tie")
        assert first == second
        category, share = first
        assert category in ("data", "media")
        assert share == pytest.approx(0.5)

    def test_uncategorized_bytes_dominate_as_other(self):
        index = build_category_index(_sample_tree())
        category, share = index.dominant("/r/junk")
        assert category == "other"
        assert share == pytest.approx(0.9)

    def test_unknown_and_empty_directories_have_no_dominant(self):
        index = build_category_index(_sample_tree())
        assert index.dominant("/r/empty") is None
        assert index.dominant("/nowhere") is None
        assert index.shares("/r/empty") == []
        assert index.shares("/nowhere") == []

    def test_files_are_not_indexed(self):
        index = build_category_index(_sample_tree())
        assert "/r/src/app.py" not in index.histograms

    def test_deep_chains_do_not_recurse(self):
        """Real trees nest deeper than the interpreter's recursion limit."""
        node = _file("leaf.py", 10, "/deep" + "/d" * 4000, 4001)
        for level in range(4000, 0, -1):
            node = _dir(f"d{level}", "/deep" + "/d" * (level - 1), level, [node])
        root = _dir("deep", "", 0, [node])
        index = build_category_index(root)
        assert index.dominant(root.path) == ("code", 1.0)


def _venv_tree() -> FSNode:
    """A checkout holding a virtualenv beside a directory the user owns.

    By extension the venv is 80% code, because installed third-party `.py`
    files are still `.py`.  That is the split — a real one measures closer
    to an even tie — that used to leave every level of a venv under the
    tint threshold and render the whole interior neutral gray.
    """
    site = "/r/.venv/lib/python3.12/site-packages"
    pkg = _dir("pkg", site, 5, [
        _file("__init__.py", 400, f"{site}/pkg", 6),
        _file("core.py", 1200, f"{site}/pkg", 6),
        _file("_speed.so", 400, f"{site}/pkg", 6),
    ])
    venv = _dir(".venv", "/r", 1, [
        _dir("lib", "/r/.venv", 2, [
            _dir("python3.12", "/r/.venv/lib", 3, [
                _dir("site-packages", "/r/.venv/lib/python3.12", 4, [pkg]),
            ]),
        ]),
    ])
    vendor = _dir("vendor", "/r", 1, [
        _file("shim.py", 1000, "/r/vendor", 2),
    ])
    return _dir("r", "", 0, [venv, vendor])


class TestEphemeralContainers:
    """Tool-minted containers classify wholesale, not by extension."""

    _VENV_LEVELS = (
        "/r/.venv",
        "/r/.venv/lib",
        "/r/.venv/lib/python3.12",
        "/r/.venv/lib/python3.12/site-packages",
        "/r/.venv/lib/python3.12/site-packages/pkg",
    )

    def test_every_level_of_a_venv_is_wholly_ephemeral(self):
        index = build_category_index(_venv_tree())
        for path in self._VENV_LEVELS:
            assert index.dominant(path) == ("ephemeral", 1.0), path

    def test_the_root_books_the_venvs_python_bytes_as_ephemeral(self):
        index = build_category_index(_venv_tree())
        shares = dict(index.shares("/r"))
        # All 2000 venv bytes, the 1600 of them that are `.py` included.
        assert shares["ephemeral"] == pytest.approx(2000 / 3000)
        assert shares["code"] == pytest.approx(1000 / 3000)

    def test_a_directory_off_the_list_keeps_its_extensions(self):
        """`vendor` is a name a user owns; only tool-minted names qualify."""
        index = build_category_index(_venv_tree())
        assert index.dominant("/r/vendor") == ("code", 1.0)

    def test_the_scan_root_itself_can_be_the_container(self):
        """Pointing disktide straight at ~/.venv must not lose the verdict."""
        root = _dir(".venv", "/home/u", 0, [
            _dir("bin", "/home/u/.venv", 1, [
                _file("activate.sh", 500, "/home/u/.venv/bin", 2),
            ]),
            _file("pyvenv.cfg", 500, "/home/u/.venv", 1),
        ])
        index = build_category_index(root)
        assert index.dominant("/home/u/.venv") == ("ephemeral", 1.0)
        assert index.dominant("/home/u/.venv/bin") == ("ephemeral", 1.0)

    def test_contained_directories_are_named_for_leaf_colouring(self):
        index = build_category_index(_venv_tree())
        assert set(index.ephemeral_dirs) == set(self._VENV_LEVELS)
        assert index.file_is_ephemeral(f"{self._VENV_LEVELS[-1]}/core.py")
        assert not index.file_is_ephemeral("/r/vendor/shim.py")

    def test_ordinary_trees_gain_no_containers(self):
        """`junk` and `nested` read like caches but are not on the list."""
        index = build_category_index(_sample_tree())
        assert index.ephemeral_dirs == frozenset()
        assert not index.file_is_ephemeral("/r/junk/build.log")


class TestContainerLeavesRenderEphemeral:
    """A venv wedge stays one colour instead of splitting into `.py` slivers."""

    @staticmethod
    def _pkg_file(tree: FSNode) -> FSNode:
        node = tree.children[0]
        while node.is_dir:
            node = node.children[0]
        return node

    def test_a_file_arc_inside_the_container_takes_the_container_colour(self):
        tree = _venv_tree()
        index = build_category_index(tree)
        layout = compute_sunburst(
            tree, 100, 46, max_depth=6, panel_bg=PANEL_BG, category_index=index,
        )
        arc = next(
            arc for arc in layout.arcs
            if arc.node.path.endswith("/pkg/core.py")
        )
        assert _arc_color(arc, index) == category_file_color("ephemeral", arc.depth)

    def test_the_same_arc_without_an_index_keeps_its_extension(self):
        tree = _venv_tree()
        layout = compute_sunburst(tree, 100, 46, max_depth=6, panel_bg=PANEL_BG)
        arc = next(
            arc for arc in layout.arcs
            if arc.node.path.endswith("/pkg/core.py")
        )
        assert _arc_color(arc) == category_file_color("code", arc.depth)

    def test_a_file_rect_inside_the_container_takes_the_container_colour(self):
        tree = _venv_tree()
        index = build_category_index(tree)
        leaf = self._pkg_file(tree)
        assert _rect_bg(leaf, 4, True, None, index) == category_file_color(
            "ephemeral", 4
        )

    def test_the_same_rect_without_an_index_keeps_its_extension(self):
        leaf = self._pkg_file(_venv_tree())
        assert _rect_bg(leaf, 4, True) == category_file_color("code", 4)

    def test_a_file_outside_the_container_is_untouched(self):
        tree = _venv_tree()
        index = build_category_index(tree)
        shim = tree.children[1].children[0]
        assert _rect_bg(shim, 2, True, None, index) == category_file_color("code", 2)


class TestDirectoryTint:
    def test_below_half_the_bytes_stays_neutral(self):
        assert category_dir_tint("code", 0.49, 2) == neutral_dir_color(2)
        assert category_dir_tint("code", 0.0, 2) == neutral_dir_color(2)

    def test_tint_strengthens_with_share(self):
        neutral = _parse_rgb(neutral_dir_color(2))
        weak = _parse_rgb(category_dir_tint("code", 0.55, 2))
        strong = _parse_rgb(category_dir_tint("code", 1.0, 2))

        def distance(color):
            return sum((color[i] - neutral[i]) ** 2 for i in range(3))

        assert 0 < distance(weak) < distance(strong)

    def test_a_pure_directory_is_still_not_a_file(self):
        """The tint says 'directory of code', never 'a code file'."""
        assert category_dir_tint("code", 1.0, 2) != category_file_color("code", 2)

    def test_mono_never_tints(self):
        set_color_scheme("mono")
        assert category_dir_tint("media", 1.0, 1) == neutral_dir_color(1)

    def test_theme_changes_the_neutral_not_the_category(self):
        set_color_scheme("warm")
        warm_file = category_file_color("code", 2)
        warm_neutral = neutral_dir_color(2)
        set_color_scheme("cold")
        assert category_file_color("code", 2) == warm_file
        assert neutral_dir_color(2) != warm_neutral


class TestSunburstUsesTheIndex:
    @staticmethod
    def _tree():
        return _dir("r", "", 0, [
            _dir("code", "/r", 1, [_file("a.py", 1000, "/r/code", 2)]),
            _dir("pics", "/r", 1, [_file("a.png", 1000, "/r/pics", 2)]),
        ])

    def _arc(self, layout, path):
        return next(arc for arc in layout.arcs if arc.node.path == path)

    def test_dominated_dirs_diverge_from_the_neutral(self):
        tree = self._tree()
        index = build_category_index(tree)
        neutral = _parse_rgb(neutral_dir_color(1))
        layout = compute_sunburst(
            tree, 100, 46, panel_bg=PANEL_BG, category_index=index,
        )
        for path in ("/r/code", "/r/pics"):
            arc = self._arc(layout, path)
            # Untinted, the two arcs are the neutral ladder plus at most the
            # sibling zebra nudge; the tint has to move further than that.
            plain = _parse_rgb(_arc_color(arc))
            assert max(abs(plain[i] - neutral[i]) for i in range(3)) <= 8
            tinted = _parse_rgb(_arc_color(arc, index))
            assert max(abs(tinted[i] - neutral[i]) for i in range(3)) > 20

    def test_dirs_of_different_content_no_longer_share_a_colour(self):
        tree = self._tree()
        index = build_category_index(tree)
        layout = compute_sunburst(
            tree, 100, 46, panel_bg=PANEL_BG, category_index=index,
        )
        code = _arc_color(self._arc(layout, "/r/code"), index)
        pics = _arc_color(self._arc(layout, "/r/pics"), index)
        assert code != pics

    def test_selected_arc_is_brighter_than_the_same_arc_unselected(self):
        tree = self._tree()
        layout = compute_sunburst(tree, 100, 46, panel_bg=PANEL_BG)
        arc = self._arc(layout, "/r/code")
        assert not arc.selected
        plain = _parse_rgb(_arc_color(arc))
        arc.selected = True
        lifted = _parse_rgb(_arc_color(arc))
        assert all(lifted[i] > plain[i] for i in range(3)), (
            f"selection left {plain} at {lifted}: the cursor's arc is not "
            f"distinguishable in current mode"
        )

    def test_center_label_uses_the_widget_background(self):
        layout = compute_sunburst(
            self._tree(), 100, 46, panel_bg=(12, 20, 33),
        )
        center = layout.labels[0]
        assert center.bg == "rgb(12,20,33)"

    def test_legend_names_the_shares_when_the_index_is_present(self):
        tree = self._tree()
        index = build_category_index(tree)
        layout = compute_sunburst(
            tree, 100, 46, panel_bg=PANEL_BG, category_index=index,
        )
        entries = [text.strip() for row in layout.legend_lines for text, _c in row]
        assert entries == ["■ code 50%", "■ media 50%"]

    def test_legend_falls_back_to_presence_without_an_index(self):
        layout = compute_sunburst(self._tree(), 100, 46, panel_bg=PANEL_BG)
        entries = [text.strip() for row in layout.legend_lines for text, _c in row]
        assert entries == ["■ code", "■ media"]

    def test_legend_is_capped_at_three_rows(self):
        children = []
        for index, (name, size) in enumerate([
            ("a.py", 700), ("b.md", 600), ("c.parquet", 500),
            ("d.png", 400), ("e.zip", 300), ("f.log", 200), ("g", 100),
        ]):
            children.append(_file(name, size, "/r", 1))
        tree = _dir("r", "", 0, children)
        layout = compute_sunburst(
            tree, 100, 46, panel_bg=PANEL_BG,
            category_index=build_category_index(tree),
        )
        assert len(layout.legend_lines) == 3
        assert sum(len(row) for row in layout.legend_lines) == 6


class TestTreemapUsesTheIndex:
    @staticmethod
    def _tree():
        return _dir("r", "", 0, [
            _dir("a", "/r", 1, [
                _dir("code", "/r/a", 2, [_file("x.py", 1000, "/r/a/code", 3)]),
                _dir("pics", "/r/a", 2, [_file("x.png", 1000, "/r/a/pics", 3)]),
            ]),
        ])

    def test_dir_leaves_are_tinted_by_what_they_hold(self):
        tree = self._tree()
        index = build_category_index(tree)
        plain = _rect_bg(tree.children[0].children[0], 2, True)
        tinted = _rect_bg(
            tree.children[0].children[0], 2, True, None, index,
        )
        assert plain != tinted
        assert tinted == category_dir_tint("code", 1.0, 2)

    def test_the_index_reaches_render_through_the_layout(self):
        tree = self._tree()
        layout = compute_layout(
            tree, 60, 24, max_depth=2,
            category_index=build_category_index(tree),
        )
        assert layout.category_index is not None
        dir_leaves = [r for r in layout.rects if r.is_leaf and r.node.is_dir]
        backgrounds = {
            _rect_bg(r.node, r.depth, True, None, layout.category_index)
            for r in dir_leaves
        }
        assert len(backgrounds) >= 2, (
            "code and image directories rendered the same background"
        )


def _scan_dir(tmp_path) -> None:
    """A source directory and a media directory, each clearly dominated."""
    code = tmp_path / "code"
    code.mkdir()
    (code / "app.py").write_text("x" * 4000)
    pics = tmp_path / "pics"
    pics.mkdir()
    (pics / "shot.png").write_bytes(b"\x89PNG" + b"y" * 4000)


class TestExplorerWiring:
    """The index is built off the main thread and only for finished scans."""

    @staticmethod
    async def _settled(pilot, app):
        await pilot.pause(delay=0.2)
        for _ in range(40):
            await pilot.pause(delay=0.1)
            screen = app.screen
            if not isinstance(screen, ExplorerScreen) or screen._root is None:
                continue
            view = screen.query_one("#sunburst-view", SunburstView)
            if view._category_index is not None:
                return screen
        raise AssertionError("the explorer never published a category index")

    def test_both_views_get_the_finished_scan_index(self, tmp_path):
        _scan_dir(tmp_path)

        async def go():
            app = DiskTideApp(
                scan_path=str(tmp_path), show_welcome=False, config=load_config()
            )
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await self._settled(pilot, app)
                index = screen.query_one("#sunburst-view", SunburstView)._category_index
                assert (
                    screen.query_one("#treemap-view", TreemapView)._category_index
                    is index
                )
                assert index.dominant(str(tmp_path / "code")) == ("code", 1.0)
                assert index.dominant(str(tmp_path / "pics"))[0] == "media"

        asyncio.run(go())

    def test_a_new_scan_drops_the_previous_index(self, tmp_path):
        """Partial data would name a dominant category from whatever landed."""
        _scan_dir(tmp_path)

        async def go():
            app = DiskTideApp(
                scan_path=str(tmp_path), show_welcome=False, config=load_config()
            )
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await self._settled(pilot, app)
                for view_id, view_cls in (
                    ("#sunburst-view", SunburstView),
                    ("#treemap-view", TreemapView),
                ):
                    screen.query_one(view_id, view_cls).set_node(None)
                    assert (
                        screen.query_one(view_id, view_cls)._category_index is None
                    )

        asyncio.run(go())

    def test_a_stale_worker_result_is_dropped(self, tmp_path):
        """A newer scan's root wins; the older pass must not overwrite it."""
        _scan_dir(tmp_path)

        async def go():
            app = DiskTideApp(
                scan_path=str(tmp_path), show_welcome=False, config=load_config()
            )
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await self._settled(pilot, app)
                view = screen.query_one("#sunburst-view", SunburstView)
                view.set_category_index(None)
                stale = FSNode(name="gone", path="/gone", is_dir=True)
                screen._apply_category_index(
                    stale, build_category_index(stale)
                )
                assert view._category_index is None

        asyncio.run(go())
