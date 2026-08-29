"""Treemap rendering tests across diverse folder patterns.

Each test builds a realistic FSNode tree, renders the treemap at one or more
viewport sizes, and asserts structural invariants that catch the classes of
bugs visible as dark gaps, broken segments, or unstyled cells.
"""

from __future__ import annotations

import pytest
from rich.style import Style

from disktide.models.tree import FSNode
from disktide.viz.colors import file_category as _file_category
from disktide.viz.treemap import (
    TreemapLayout,
    compute_layout,
    render_line,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_file(name: str, size: int, parent_path: str = "/root", depth: int = 1) -> FSNode:
    return FSNode(
        name=name, path=f"{parent_path}/{name}",
        size=size, is_dir=False, depth=depth,
    )


def _make_dir(
    name: str,
    children: list[FSNode],
    parent_path: str = "/root",
    depth: int = 1,
) -> FSNode:
    total = sum(c.size for c in children)
    d = FSNode(
        name=name, path=f"{parent_path}/{name}",
        size=total, own_size=total, is_dir=True, depth=depth,
        children=children,
    )
    return d


def _wrap_root(children: list[FSNode], name: str = "root") -> FSNode:
    total = sum(c.size for c in children)
    return FSNode(
        name=name, path=f"/{name}",
        size=total, own_size=total, is_dir=True, depth=0,
        children=children,
    )


# ---------------------------------------------------------------------------
# Invariant checker — the core of every test
# ---------------------------------------------------------------------------

def _count_parent_bleed(layout: TreemapLayout, max_depth: int = 3) -> int:
    """Count cells in a parent rect's padded inner area that still show the parent.

    These appear as dark border-colored gaps where content should be.
    """
    bleed = 0
    for prect in layout.rects:
        if prect.is_leaf:
            continue
        pad = 1 if prect.depth < max_depth - 1 and prect.w >= 6 and prect.h >= 5 else 0
        if pad == 0:
            continue
        ix0 = int(prect.x) + pad
        iy0 = int(prect.y) + pad
        ix1 = min(layout.width, int(prect.x + prect.w) - pad)
        iy1 = min(layout.height, int(prect.y + prect.h) - pad)
        for y in range(iy0, iy1):
            for x in range(ix0, ix1):
                cell = layout.rect_at(x, y)
                if cell is prect:
                    bleed += 1
    return bleed


def _owned_cells(layout: TreemapLayout) -> dict[int, int]:
    """Map id(rect) -> number of grid cells that actually point at it."""
    owned: dict[int, int] = {}
    for row in layout.grid:
        for cell in row:
            if cell is not None:
                owned[id(cell)] = owned.get(id(cell), 0) + 1
    return owned


def _aggregate_count(name: str) -> int:
    """Number of children folded into an '… N more' aggregate node."""
    return int(name.split()[1].replace(",", ""))


def _visual_aspect(rect) -> float:
    """Aspect ratio as it appears on screen: a cell is ~2x taller than wide."""
    w, vh = rect.w, rect.h * 2
    return max(w, vh) / min(w, vh)


def _border_ratio(layout: TreemapLayout) -> float:
    """Fraction of cells showing border (non-leaf) rects or None."""
    total = layout.width * layout.height
    if total == 0:
        return 0.0
    border = 0
    for row in layout.grid:
        for cell in row:
            if cell is None or not cell.is_leaf:
                border += 1
    return border / total


def assert_layout_integrity(
    layout: TreemapLayout,
    *,
    allow_none_cells: bool = False,
    max_depth: int = 3,
) -> None:
    """Assert every structural invariant a well-formed treemap must satisfy.

    Checks:
    1. Grid dimensions match declared width/height.
    2. No None cells in the grid (unless explicitly allowed).
    3. Every rendered line has exactly `width` characters total.
    4. Every segment carries a bgcolor (no unstyled gaps).
    5. All rects lie within the grid bounds.
    6. No parent bleeding in padded inner areas.
    7. No leaf rect is fragmented or hidden by an overlapping neighbour.
    """
    w, h = layout.width, layout.height

    # --- grid shape ---
    assert len(layout.grid) == h, (
        f"Grid has {len(layout.grid)} rows, expected {h}"
    )
    for y, row in enumerate(layout.grid):
        assert len(row) == w, (
            f"Grid row {y} has {len(row)} cols, expected {w}"
        )

    # --- no None cells ---
    if not allow_none_cells:
        for y, row in enumerate(layout.grid):
            for x, cell in enumerate(row):
                assert cell is not None, (
                    f"None cell at ({x}, {y}) — gap in the treemap"
                )

    # --- rect bounds ---
    for rect in layout.rects:
        assert rect.x >= 0, f"rect.x={rect.x} < 0"
        assert rect.y >= 0, f"rect.y={rect.y} < 0"
        assert rect.x + rect.w <= w + 0.5, (
            f"rect right edge {rect.x + rect.w} > width {w}"
        )
        assert rect.y + rect.h <= h + 0.5, (
            f"rect bottom edge {rect.y + rect.h} > height {h}"
        )

    # --- no parent bleed ---
    bleed = _count_parent_bleed(layout, max_depth)
    assert bleed == 0, (
        f"{bleed} cells show parent rect in padded inner area — "
        f"border color where content should be"
    )

    # --- no fragmentation ---
    # Rects tile their parent, so every leaf should own every cell it
    # covers.  The one legitimate loss is _snap_rects inflating a
    # zero-width neighbour to the minimum 1 cell, which can shave a single
    # row or column off this rect — at most max(w, h) cells.  More than
    # that means rects are overlapping and drawing over each other, which
    # shows as broken blocks and vanished files.  Skipped on viewports too
    # small to lay anything out, where inflation is the whole story.
    if w >= 8 and h >= 6:
        owned = _owned_cells(layout)
        for rect in layout.rects:
            if not rect.is_leaf:
                continue
            rw, rh = int(rect.w), int(rect.h)
            area = rw * rh
            if area <= 0:
                continue
            visible = owned.get(id(rect), 0)
            assert visible > 0, (
                f"Leaf {rect.node.name!r} ({rw}x{rh} at "
                f"{int(rect.x)},{int(rect.y)}) claims cells but shows none — "
                f"an overlapping rect was drawn over all of it"
            )
            assert visible >= area - max(rw, rh), (
                f"Leaf {rect.node.name!r} ({rw}x{rh}, area {area}) shows only "
                f"{visible} cells — fragmented by an overlapping neighbour"
            )

    # --- rendered lines ---
    for y in range(h):
        segments = render_line(layout, y)
        total_chars = sum(len(seg.text) for seg in segments)
        assert total_chars == w, (
            f"Line {y}: rendered {total_chars} chars, expected {w}"
        )
        # every segment must have a background color
        for i, seg in enumerate(segments):
            assert seg.style is not None, (
                f"Line {y}, segment {i}: style is None"
            )
            assert seg.style.bgcolor is not None, (
                f"Line {y}, segment {i}: no bgcolor — would render as a gap"
            )


def _render_full(layout: TreemapLayout) -> list[str]:
    """Render the entire treemap as a list of text lines (for debugging)."""
    lines = []
    for y in range(layout.height):
        segs = render_line(layout, y)
        lines.append("".join(s.text for s in segs))
    return lines


# ---------------------------------------------------------------------------
# Viewport sizes used across tests — includes odd dimensions, tiny, and large
# ---------------------------------------------------------------------------

VIEWPORTS = [
    (80, 24),   # standard terminal
    (120, 40),  # large terminal
    (40, 12),   # small pane
    (79, 37),   # prime-ish, exposes rounding issues
    (11, 5),    # very tight
]


# ---------------------------------------------------------------------------
# Folder pattern fixtures
# ---------------------------------------------------------------------------

class TestSingleFile:
    """A root directory containing exactly one file."""

    @staticmethod
    def _tree():
        return _wrap_root([_make_file("huge.bin", 1_000_000)])

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)

    def test_fills_entire_area(self):
        layout = compute_layout(self._tree(), 40, 20)
        # The file rect should cover (nearly) every cell
        leaf_rects = [r for r in layout.rects if r.is_leaf]
        assert len(leaf_rects) == 1


class TestManyTinyFiles:
    """50 tiny files in a single directory — stress-tests squarify subdivision."""

    @staticmethod
    def _tree():
        files = [_make_file(f"f{i:02d}.log", 100 + i) for i in range(50)]
        return _wrap_root(files)

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)


class TestOneDominantFile:
    """One file is 95% of total size, rest are tiny — extreme aspect ratios."""

    @staticmethod
    def _tree():
        big = _make_file("database.sqlite", 950_000)
        smalls = [_make_file(f"s{i}.txt", 500) for i in range(100)]
        return _wrap_root([big] + smalls)

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)

    def test_dominant_file_gets_most_area(self):
        layout = compute_layout(self._tree(), 80, 24)
        # Find the big file's leaf rect
        big_rects = [r for r in layout.rects if r.is_leaf and r.node.name == "database.sqlite"]
        assert len(big_rects) == 1
        big = big_rects[0]
        area = big.w * big.h
        total_area = layout.width * layout.height
        assert area / total_area > 0.5, "Dominant file should take most of the area"


class TestDeepNesting:
    """5 levels of single-child directories — tests padding accumulation."""

    @staticmethod
    def _tree():
        leaf = _make_file("deep.py", 5000, parent_path="/root/a/b/c/d", depth=5)
        d = _make_dir("d", [leaf], parent_path="/root/a/b/c", depth=4)
        c = _make_dir("c", [d], parent_path="/root/a/b", depth=3)
        b = _make_dir("b", [c], parent_path="/root/a", depth=2)
        a = _make_dir("a", [b], parent_path="/root", depth=1)
        return _wrap_root([a])

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h, max_depth=5)
        assert_layout_integrity(layout)

    def test_default_max_depth(self):
        """At default max_depth=3, deep dirs become leaf rects."""
        layout = compute_layout(self._tree(), 80, 24, max_depth=3)
        assert_layout_integrity(layout)


class TestWideFlatTopLevel:
    """15 top-level directories each with 2 files — many color groups."""

    @staticmethod
    def _tree():
        dirs = []
        for i in range(15):
            ext = ["py", "pdf", "png", "csv", "mp3", "json", "zip", "c"][i % 8]
            files = [
                _make_file(f"a.{ext}", 1000 + i * 100, parent_path=f"/root/d{i:02d}", depth=2),
                _make_file(f"b.{ext}", 800 + i * 50, parent_path=f"/root/d{i:02d}", depth=2),
            ]
            dirs.append(_make_dir(f"d{i:02d}", files, depth=1))
        return _wrap_root(dirs)

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)


class TestUnbalancedBinary:
    """Recursively one big + one small child — extreme layout skew."""

    @staticmethod
    def _tree():
        # Build bottom-up: at each level, big child is 5x the small one
        leaf_big = _make_file("big.rs", 8000, parent_path="/root/L/L/L", depth=4)
        leaf_small = _make_file("small.rs", 200, parent_path="/root/L/L/L", depth=4)
        d3 = _make_dir("L", [leaf_big, leaf_small], parent_path="/root/L/L", depth=3)
        s3 = _make_file("tiny.txt", 100, parent_path="/root/L/L", depth=3)
        d2 = _make_dir("L", [d3, s3], parent_path="/root/L", depth=2)
        s2 = _make_file("t.cfg", 50, parent_path="/root/L", depth=2)
        d1 = _make_dir("L", [d2, s2], parent_path="/root", depth=1)
        s1 = _make_file("readme.md", 30, parent_path="/root", depth=1)
        return _wrap_root([d1, s1])

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)


class TestAllSameSize:
    """Every file is exactly the same size — perfectly even division."""

    @staticmethod
    def _tree():
        files = [_make_file(f"file{i:02d}.py", 1000) for i in range(12)]
        return _wrap_root(files)

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)


class TestMixedFileTypes:
    """Files with diverse extensions — verifies color differentiation."""

    @staticmethod
    def _tree():
        files = [
            _make_file("report.pdf", 5000),
            _make_file("photo.jpg", 4000),
            _make_file("main.py", 3000),
            _make_file("config.yaml", 2000),
            _make_file("data.csv", 6000),
            _make_file("backup.tar.gz", 8000),
            _make_file("song.mp3", 7000),
            _make_file("module.pyc", 1000),
            _make_file("LICENSE", 500),
        ]
        return _wrap_root(files)

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)

    def test_distinct_colors_per_type(self):
        """Different file-type categories should produce different bg colors."""
        layout = compute_layout(self._tree(), 80, 24)
        leaf_bgs: dict[str, set[str]] = {}
        for rect in layout.rects:
            if not rect.is_leaf:
                continue
            cat = _file_category(rect.node.name)
            # Collect the style that would be rendered
            from disktide.viz.treemap import _rect_bg
            bg = _rect_bg(rect.node, rect.depth, rect.is_leaf)
            leaf_bgs.setdefault(cat, set()).add(bg)

        # At least 4 distinct categories should be represented
        assert len(leaf_bgs) >= 4, f"Only {len(leaf_bgs)} categories: {list(leaf_bgs)}"
        # Categories with the same depth should map to different colors
        all_bgs = set()
        for cat, bgs in leaf_bgs.items():
            all_bgs.update(bgs)
        assert len(all_bgs) >= 4, "Expected at least 4 distinct background colors"


class TestDirectoryLeafAtMaxDepth:
    """A dir node hit at max_depth — it becomes a leaf styled as neutral gray."""

    @staticmethod
    def _tree():
        deep_files = [_make_file(f"f{i}.c", 500, parent_path="/root/a/b/c", depth=4) for i in range(5)]
        c = _make_dir("c", deep_files, parent_path="/root/a/b", depth=3)
        b = _make_dir("b", [c], parent_path="/root/a", depth=2)
        a = _make_dir("a", [b], parent_path="/root", depth=1)
        return _wrap_root([a])

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h, max_depth=3)
        assert_layout_integrity(layout)

    def test_dir_leaf_exists(self):
        layout = compute_layout(self._tree(), 80, 24, max_depth=3)
        dir_leaves = [r for r in layout.rects if r.is_leaf and r.node.is_dir]
        assert len(dir_leaves) >= 1, "Should have at least one directory leaf"


class TestMinimalViewports:
    """Extremely small viewport sizes that push the layout to edge cases."""

    @staticmethod
    def _tree():
        return _wrap_root([
            _make_file("a.py", 500),
            _make_file("b.py", 300),
        ])

    @pytest.mark.parametrize("w,h", [
        (1, 1),
        (2, 1),
        (1, 2),
        (3, 2),
        (2, 3),
        (4, 4),
    ])
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)

    @pytest.mark.parametrize("w,h", [(0, 10), (10, 0), (0, 0), (-1, 10)])
    def test_zero_or_negative_produces_empty(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert len(layout.rects) == 0


class TestTypicalProject:
    """Simulates a realistic Python project directory structure."""

    @staticmethod
    def _tree():
        src_files = [
            _make_file("main.py", 12000, parent_path="/proj/src", depth=2),
            _make_file("utils.py", 8000, parent_path="/proj/src", depth=2),
            _make_file("config.yaml", 2000, parent_path="/proj/src", depth=2),
            _make_file("models.py", 15000, parent_path="/proj/src", depth=2),
            _make_file("api.py", 9000, parent_path="/proj/src", depth=2),
        ]
        src = _make_dir("src", src_files, parent_path="/proj", depth=1)

        test_files = [
            _make_file("test_main.py", 6000, parent_path="/proj/tests", depth=2),
            _make_file("test_utils.py", 4000, parent_path="/proj/tests", depth=2),
            _make_file("conftest.py", 1500, parent_path="/proj/tests", depth=2),
        ]
        tests = _make_dir("tests", test_files, parent_path="/proj", depth=1)

        doc_files = [
            _make_file("guide.pdf", 50000, parent_path="/proj/docs", depth=2),
            _make_file("arch.png", 25000, parent_path="/proj/docs", depth=2),
            _make_file("notes.md", 3000, parent_path="/proj/docs", depth=2),
        ]
        docs = _make_dir("docs", doc_files, parent_path="/proj", depth=1)

        top_files = [
            _make_file("pyproject.toml", 800, parent_path="/proj", depth=1),
            _make_file("README.md", 2500, parent_path="/proj", depth=1),
        ]

        return FSNode(
            name="proj", path="/proj",
            size=sum(c.size for c in [src, tests, docs] + top_files),
            own_size=0, is_dir=True, depth=0,
            children=[src, tests, docs] + top_files,
        )

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)

    def test_labels_present_on_large_viewport(self):
        """At 120x40 there's enough room for at least some file labels."""
        layout = compute_layout(self._tree(), 120, 40)
        labeled_leaves = [r for r in layout.rects if r.is_leaf and r.label]
        assert len(labeled_leaves) >= 1, "Should label at least one leaf"

    def test_size_labels_on_tall_blocks(self):
        """Blocks tall enough should have a size label."""
        layout = compute_layout(self._tree(), 120, 40)
        sized = [r for r in layout.rects if r.is_leaf and r.size_label]
        # The big PDF (50 KB) should be tall enough at this viewport
        assert len(sized) >= 1, "Expected at least one leaf with a size label"

    def test_dir_border_labels(self):
        """Parent rects at depth 0/1 should carry directory labels."""
        layout = compute_layout(self._tree(), 120, 40)
        dir_labeled = [r for r in layout.rects if not r.is_leaf and r.label]
        assert len(dir_labeled) >= 1, "Expected at least one directory border label"


class TestBuildArtifactHeavy:
    """Project dominated by build artifacts — tests the 'ephemeral' category."""

    @staticmethod
    def _tree():
        build_files = [
            _make_file(f"module{i}.o", 20000, parent_path="/proj/build", depth=2)
            for i in range(20)
        ]
        build_files.append(
            _make_file("libfoo.so", 150000, parent_path="/proj/build", depth=2)
        )
        build = _make_dir("build", build_files, parent_path="/proj", depth=1)

        src_files = [
            _make_file(f"mod{i}.c", 3000, parent_path="/proj/src", depth=2)
            for i in range(10)
        ]
        src = _make_dir("src", src_files, parent_path="/proj", depth=1)

        return FSNode(
            name="proj", path="/proj",
            size=build.size + src.size,
            own_size=0, is_dir=True, depth=0,
            children=[build, src],
        )

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)


class TestSingleDeepFile:
    """A single file buried 4 directories deep — tests minimal content."""

    @staticmethod
    def _tree():
        f = _make_file("secret.dat", 1000, parent_path="/r/a/b/c", depth=4)
        c = _make_dir("c", [f], parent_path="/r/a/b", depth=3)
        b = _make_dir("b", [c], parent_path="/r/a", depth=2)
        a = _make_dir("a", [b], parent_path="/r", depth=1)
        return _wrap_root([a], name="r")

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h, max_depth=5)
        assert_layout_integrity(layout)


class TestLargeViewport:
    """A simple tree rendered at unusually large sizes."""

    @staticmethod
    def _tree():
        return _wrap_root([
            _make_file("big.iso", 900_000),
            _make_file("small.txt", 100),
        ])

    @pytest.mark.parametrize("w,h", [
        (200, 60),
        (300, 100),
        (500, 150),
    ])
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)


class TestFileCategoryMapping:
    """Verify the extension → category mapping covers expected cases."""

    @pytest.mark.parametrize("name,expected", [
        ("report.pdf", "docs"),
        ("REPORT.PDF", "docs"),
        ("doc.epub", "docs"),
        ("slides.pptx", "docs"),
        ("photo.jpeg", "media"),
        ("icon.heic", "media"),
        ("style.css", "code"),
        ("component.tsx", "code"),
        ("notebook.ipynb", "code"),
        ("settings.toml", "docs"),
        ("app.conf", "docs"),
        ("dump.sql", "data"),
        ("data.h5", "data"),
        ("dataset.hdf5", "data"),
        ("features.npz", "data"),
        ("model.pt", "data"),
        ("weights.safetensors", "data"),
        ("checkpoint.ckpt", "data"),
        ("archive.7z", "archive"),
        ("pkg.deb", "archive"),
        ("video.mkv", "media"),
        ("stream.ogg", "media"),
        ("module.whl", "ephemeral"),
        ("library.dll", "ephemeral"),
        ("training.log", "ephemeral"),
        ("output.out", "ephemeral"),
        ("errors.err", "ephemeral"),
        ("Makefile", "other"),
        ("no_extension", "other"),
        (".hidden", "other"),
        ("multi.tar.gz", "archive"),  # uses last extension
        # versioned shared libraries: numeric suffixes peel off (HPC trees
        # are full of libfoo.so.N and would otherwise render as "other")
        ("libcudnn.so.9", "ephemeral"),
        ("libfoo.so.1.2.3", "ephemeral"),
        ("liblapack.so.3", "ephemeral"),
        ("data.1", "other"),  # all-numeric suffixes with no real extension
    ])
    def test_category(self, name, expected):
        assert _file_category(name) == expected

    def test_every_category_has_a_color_in_every_scheme(self):
        """No theme may leave a known category without a distinct fill."""
        from disktide.viz.colors import (
            CATEGORIES,
            EXT_CATEGORIES,
            SCHEMES,
            category_file_color,
            set_color_scheme,
        )
        all_cats = set(EXT_CATEGORIES.values()) | {"other"}
        assert all_cats == set(CATEGORIES)
        try:
            for name in SCHEMES:
                set_color_scheme(name)
                colors = {cat: category_file_color(cat, 2) for cat in CATEGORIES}
                assert all(colors.values()), f"{name} scheme missing a fill"
                if name != "mono":
                    assert len(set(colors.values())) == len(CATEGORIES), (
                        f"{name} scheme reuses a fill across categories: {colors}"
                    )
        finally:
            set_color_scheme("default")


class TestNonUniformSiblings:
    """Children with wildly different sizes: 1 byte to 1 GB range."""

    @staticmethod
    def _tree():
        files = [
            _make_file("giant.iso", 1_000_000_000),
            _make_file("medium.zip", 10_000_000),
            _make_file("small.py", 10_000),
            _make_file("tiny.cfg", 100),
            _make_file("atom.txt", 1),
        ]
        return _wrap_root(files)

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)


# ---------------------------------------------------------------------------
# Bug-targeted tests — these expose specific rendering defects
# ---------------------------------------------------------------------------

class TestNoDroppedChildren:
    """Children too small for their own rect must still be represented.

    Bug: _layout_node returns early when w < 1 or h < 1, completely
    dropping the child. Its allocated cells show as parent border color.
    """

    @staticmethod
    def _tree():
        big = _make_file("database.sqlite", 950_000)
        smalls = [_make_file(f"s{i}.txt", 500) for i in range(100)]
        return _wrap_root([big] + smalls)

    def test_every_child_is_represented(self):
        """No sized child is silently dropped.

        This used to demand that at least 10 of the 100 sub-cell files each
        get a rect of their own.  Meeting that required inflating zero-area
        rects to a full cell, and consecutive inflations landed on the same
        cells: build_grid draws later over earlier, so the survivors were
        fragments and the rest were invisible.  The honest invariant is
        representation — individually, or by an aggregate that accounts for
        them — not a rect count.
        """
        tree = self._tree()
        layout = compute_layout(tree, 80, 24)
        assert_layout_integrity(layout)

        leaves = [r for r in layout.rects if r.is_leaf]
        assert any(r.node.name == "database.sqlite" for r in leaves)

        aggregates = [r for r in leaves if "more" in r.node.name]
        individual = [r for r in leaves if "more" not in r.node.name]

        represented = len(individual) + sum(
            _aggregate_count(r.node.name) for r in aggregates
        )
        assert represented == 101, (
            f"{represented} of 101 children are represented — the rest were "
            f"silently dropped"
        )
        # ...and the aggregates carry their children's real bytes, so the
        # picture still adds up to the root's size.
        assert sum(r.node.size for r in leaves) == tree.size

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_no_parent_bleed(self, w, h):
        """No parent border color inside the inner content area."""
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)


class TestBorderRatio:
    """Border cells should not dominate the treemap at reasonable viewports.

    Bug: 1-char padding is applied unconditionally, even when the rect is
    too small. Nested padding compounds: 2 levels on a 15x8 rect leaves
    69% border, 87% at 11x5.
    """

    @staticmethod
    def _project_tree():
        src = _make_dir("src", [
            _make_file("main.py", 12000, "/proj/src", 2),
            _make_file("utils.py", 8000, "/proj/src", 2),
            _make_file("models.py", 15000, "/proj/src", 2),
        ], "/proj", 1)
        docs = _make_dir("docs", [
            _make_file("guide.pdf", 50000, "/proj/docs", 2),
            _make_file("logo.png", 25000, "/proj/docs", 2),
        ], "/proj", 1)
        tests = _make_dir("tests", [
            _make_file("test_main.py", 6000, "/proj/tests", 2),
        ], "/proj", 1)
        return FSNode(
            name="proj", path="/proj",
            size=src.size + docs.size + tests.size,
            is_dir=True, depth=0,
            children=[src, docs, tests],
        )

    def test_standard_viewport_border_ratio(self):
        """At 80x24, border should be < 35% of the area."""
        layout = compute_layout(self._project_tree(), 80, 24)
        ratio = _border_ratio(layout)
        assert ratio < 0.35, (
            f"Border ratio {ratio:.1%} at 80x24 — too much padding"
        )

    def test_small_viewport_border_ratio(self):
        """At 40x12, border should be < 50% of the area."""
        layout = compute_layout(self._project_tree(), 40, 12)
        ratio = _border_ratio(layout)
        assert ratio < 0.50, (
            f"Border ratio {ratio:.1%} at 40x12 — padding eats content"
        )

    def test_tiny_viewport_border_ratio(self):
        """At 15x8, border should be <= 55% of the area."""
        layout = compute_layout(self._project_tree(), 15, 8)
        ratio = _border_ratio(layout)
        assert ratio <= 0.55, (
            f"Border ratio {ratio:.1%} at 15x8 — padding dominates"
        )

    def test_15_dirs_small_viewport(self):
        """15 directories at 40x12 should not exceed 50% border."""
        dirs = []
        for i in range(15):
            f = [_make_file(f"a.py", 1000, f"/r/d{i:02d}", 2)]
            dirs.append(_make_dir(f"d{i:02d}", f, "/r", 1))
        tree = _wrap_root(dirs, "r")
        layout = compute_layout(tree, 40, 12)
        ratio = _border_ratio(layout)
        assert ratio < 0.50, (
            f"Border ratio {ratio:.1%} for 15 dirs at 40x12"
        )


class TestContentCoverage:
    """Verify that content (leaf) rects cover a meaningful fraction of the grid.

    This is the inverse of TestBorderRatio: at any reasonable viewport, a
    significant portion of the area should show actual file content, not just
    borders and gaps.
    """

    @staticmethod
    def _tree():
        """A tree with moderate nesting and mixed sizes."""
        src = _make_dir("src", [
            _make_file("app.py", 20000, "/p/src", 2),
            _make_file("db.py", 15000, "/p/src", 2),
            _make_file("api.py", 10000, "/p/src", 2),
        ], "/p", 1)
        data = _make_dir("data", [
            _make_file("dump.csv", 80000, "/p/data", 2),
        ], "/p", 1)
        return FSNode(
            name="p", path="/p",
            size=src.size + data.size,
            is_dir=True, depth=0,
            children=[src, data],
        )

    def test_content_covers_majority_at_standard_size(self):
        layout = compute_layout(self._tree(), 80, 24)
        ratio = _border_ratio(layout)
        content_ratio = 1.0 - ratio
        assert content_ratio > 0.60, (
            f"Content only covers {content_ratio:.1%} — "
            f"most area is border/padding"
        )


class TestDeepNestingPaddingCollapse:
    """Deeply nested single-child chains should not consume all space as borders.

    Bug: each nesting level adds 1-char padding. 4 levels deep on an 11x5
    viewport leaves 3x0 inner area — nothing visible.
    """

    @staticmethod
    def _tree():
        f = _make_file("leaf.py", 1000, "/r/a/b/c", 4)
        c = _make_dir("c", [f], "/r/a/b", 3)
        b = _make_dir("b", [c], "/r/a", 2)
        a = _make_dir("a", [b], "/r", 1)
        return _wrap_root([a], "r")

    def test_leaf_visible_at_small_viewport(self):
        """The leaf file should claim at least 1 cell even at 11x5."""
        layout = compute_layout(self._tree(), 11, 5, max_depth=5)
        leaf_rects = [r for r in layout.rects if r.is_leaf and r.node.name == "leaf.py"]
        assert len(leaf_rects) >= 1, "Leaf file should produce a rect"
        # The leaf should actually appear in the grid
        leaf_rect = leaf_rects[0]
        found = False
        for row in layout.grid:
            for cell in row:
                if cell is leaf_rect:
                    found = True
                    break
            if found:
                break
        assert found, "Leaf rect exists but claims no grid cells — padding consumed all space"

    def test_border_ratio_at_tiny_viewport(self):
        """At 11x5 with max_depth=5, border should be < 80%."""
        layout = compute_layout(self._tree(), 11, 5, max_depth=5)
        ratio = _border_ratio(layout)
        assert ratio < 0.80, (
            f"Border ratio {ratio:.1%} at 11x5 max_depth=5 — "
            f"padding consumed almost everything"
        )


class TestHighlySkewedDistribution:
    """1 large + 50 tiny nodes — no None cells, bounded aspect ratios."""

    @staticmethod
    def _tree():
        big = _make_file("huge.bin", 1_000_000)
        tiny = [_make_file(f"t{i:02d}.txt", 10) for i in range(50)]
        return _wrap_root([big] + tiny)

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_no_extreme_visual_aspect_ratios(self, w, h):
        """Bound the aspect ratio as it appears on screen.

        The old check bounded raw cell dimensions at 20:1 and passed only
        because the overlapping inflated rects had collapsed all 50 slivers
        into 1x1s, half of them invisible.  A terminal cell is about twice
        as tall as it is wide, so a w x h rect reads as w x 2h; that is the
        ratio squarify now optimizes and the one worth asserting.

        The crumbs are consolidated into a single "… N more" block per
        parent.  Its thickness is set by the metric it honestly encodes,
        not by the layout, so it is exempt — but only one such block per
        parent is allowed, otherwise the sliver stack is simply back.
        """
        layout = compute_layout(self._tree(), w, h)
        thin_aggregates: dict[str, int] = {}
        for rect in layout.rects:
            if not rect.is_leaf:
                continue
            if rect.w < 1 or rect.h < 1 or int(rect.w) * int(rect.h) < 4:
                continue
            ratio = _visual_aspect(rect)
            if "more" in rect.node.name and ratio > 4:
                parent = rect.node.path.rsplit("/", 1)[0]
                thin_aggregates[parent] = thin_aggregates.get(parent, 0) + 1
                continue
            assert ratio <= 4, (
                f"Rect {rect.node.name} has visual aspect ratio {ratio:.1f} "
                f"({rect.w}x{rect.h} cells)"
            )
        for parent, count in thin_aggregates.items():
            assert count == 1, (
                f"{parent} emitted {count} thin aggregate strips — "
                f"consolidation should leave at most one"
            )


class TestSkewedRealisticProject:
    """The shape that produced the broken screenshot: one dominant subtree
    per level plus a tail of crumbs, at a full-screen viewport.

    Before consolidation this laid 'bin' out as a 1x35 strip, overwrote 13
    of 'share''s 14 cells with a neighbour's inflated rect, and rendered
    'd.tcss' and 'pyproject.toml' nowhere at all.
    """

    KIB = 1024
    MIB = 1024 * 1024

    @classmethod
    def _tree(cls):
        KiB, MiB = cls.KIB, cls.MIB
        venv = _make_dir(".venv", [
            _make_dir("lib", [
                _make_file("python3.12", int(29.9 * MiB), "/p/.venv/lib", 3),
            ], "/p/.venv", 2),
            _make_file("bin", 300 * KiB, "/p/.venv", 2),
            _make_file("share", 120 * KiB, "/p/.venv", 2),
            _make_file("pyvenv.cfg", 1 * KiB, "/p/.venv", 2),
        ], "/p", 1)
        git = _make_dir(".git", [
            _make_file("pack", int(4.9 * MiB), "/p/.git", 2),
            _make_file("objects", 300 * KiB, "/p/.git", 2),
            _make_file("refs", 40 * KiB, "/p/.git", 2),
        ], "/p", 1)
        src = _make_dir("src", [
            _make_file("rest.py", 1600 * KiB, "/p/src", 2),
            _make_file("screens.py", 409 * KiB, "/p/src", 2),
            _make_file("storage.py", 336 * KiB, "/p/src", 2),
            _make_file("viz.py", 250 * KiB, "/p/src", 2),
            _make_file("app.py", 200 * KiB, "/p/src", 2),
        ], "/p", 1)
        tests = _make_dir("tests", [
            _make_file("golden.png", int(2.2 * MiB), "/p/tests", 2),
            _make_file("t1.py", 200 * KiB, "/p/tests", 2),
            _make_file("t2.py", 200 * KiB, "/p/tests", 2),
        ], "/p", 1)
        docs = _make_dir("docs", [
            _make_file("a.jpg", 700 * KiB, "/p/docs", 2),
            _make_file("b.jpg", 500 * KiB, "/p/docs", 2),
            _make_file("c.md", 300 * KiB, "/p/docs", 2),
        ], "/p", 1)
        children = [
            venv, git, src, tests, docs,
            _make_file("uv.lock", 223 * KiB, "/p", 1),
            _make_dir("tool", [_make_file("x.py", 106 * KiB, "/p/tool", 2)], "/p", 1),
            _make_dir(
                ".pytest_cache",
                [_make_file("v", 68 * KiB, "/p/.pytest_cache", 2)], "/p", 1,
            ),
            _make_file("README.md", 9 * KiB, "/p", 1),
            _make_dir(
                ".github",
                [_make_file("ci.yml", 7 * KiB, "/p/.github", 2)], "/p", 1,
            ),
            _make_dir(
                "assets",
                [_make_file("d.tcss", 2 * KiB, "/p/assets", 2)], "/p", 1,
            ),
            _make_file("pyproject.toml", int(1.5 * KiB), "/p", 1),
            _make_file(".gitignore", 304, "/p", 1),
        ]
        return _wrap_root(children, name="p")

    @pytest.mark.parametrize("w,h", VIEWPORTS + [(128, 53)])
    def test_integrity(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        assert_layout_integrity(layout)

    def test_no_fragmented_or_hidden_leaves(self):
        """Every leaf owns every cell of its snapped rect."""
        layout = compute_layout(self._tree(), 128, 53)
        owned = _owned_cells(layout)
        fragmented, hidden = [], []
        for rect in layout.rects:
            if not rect.is_leaf:
                continue
            area = int(rect.w) * int(rect.h)
            if area <= 0:
                continue
            visible = owned.get(id(rect), 0)
            if visible == 0:
                hidden.append((rect.node.name, int(rect.w), int(rect.h)))
            elif visible < area:
                fragmented.append(
                    (rect.node.name, int(rect.w), int(rect.h), visible)
                )
        assert hidden == [], f"leaves rendered nowhere: {hidden}"
        assert fragmented == [], f"leaves partly overwritten: {fragmented}"

    def test_no_adjacent_sibling_slivers(self):
        """No two 1-cell-thin siblings sit side by side — no sliver stacks."""
        layout = compute_layout(self._tree(), 128, 53)
        thin = [
            r for r in layout.rects
            if r.is_leaf and min(int(r.w), int(r.h)) <= 1
        ]
        for i, a in enumerate(thin):
            for b in thin[i + 1:]:
                if a.node.path.rsplit("/", 1)[0] != b.node.path.rsplit("/", 1)[0]:
                    continue
                touch_x = (
                    int(a.x) + int(a.w) == int(b.x)
                    or int(b.x) + int(b.w) == int(a.x)
                )
                touch_y = (
                    int(a.y) + int(a.h) == int(b.y)
                    or int(b.y) + int(b.h) == int(a.y)
                )
                overlap_y = (
                    int(a.y) < int(b.y) + int(b.h)
                    and int(b.y) < int(a.y) + int(a.h)
                )
                overlap_x = (
                    int(a.x) < int(b.x) + int(b.w)
                    and int(b.x) < int(a.x) + int(a.w)
                )
                assert not ((touch_x and overlap_y) or (touch_y and overlap_x)), (
                    f"sliver stack: {a.node.name!r} "
                    f"({int(a.w)}x{int(a.h)} at {int(a.x)},{int(a.y)}) is "
                    f"adjacent to {b.node.name!r} "
                    f"({int(b.w)}x{int(b.h)} at {int(b.x)},{int(b.y)})"
                )

    def test_crumbs_are_consolidated_and_labeled(self):
        """The crumb-heavy parents get a labeled '… N more' block."""
        layout = compute_layout(self._tree(), 128, 53)
        aggregates = {
            r.node.path.rsplit("/", 1)[0]: r
            for r in layout.rects
            if r.is_leaf and "more" in r.node.name
        }
        # .venv (bin/share/pyvenv.cfg behind a 29.9 MiB sibling) and the
        # root's tail of sub-10 KiB entries are the crumb-heavy parents.
        assert "/p/.venv" in aggregates, f"no aggregate under /p/.venv: {aggregates}"
        assert "/p" in aggregates, f"no aggregate at the root: {aggregates}"
        for parent, rect in aggregates.items():
            assert _aggregate_count(rect.node.name) >= 2
            assert rect.node.is_dir, "aggregates use the neutral dir-leaf color"
        # The wide ones are wide enough to actually show their label.
        wide = [r for r in aggregates.values() if r.w >= 8]
        assert wide and all(r.label for r in wide)


class TestIntegerCoordinates:
    """All rects should have integer coordinates after layout."""

    @staticmethod
    def _tree():
        files = [
            _make_file("a.py", 5000),
            _make_file("b.py", 3000),
            _make_file("c.py", 2000),
            _make_file("d.py", 1000),
        ]
        src = _make_dir("src", files)
        return _wrap_root([src])

    @pytest.mark.parametrize("w,h", VIEWPORTS)
    def test_all_coords_are_integers(self, w, h):
        layout = compute_layout(self._tree(), w, h)
        for rect in layout.rects:
            assert rect.x == int(rect.x), f"rect.x={rect.x} is not integer"
            assert rect.y == int(rect.y), f"rect.y={rect.y} is not integer"
            assert rect.w == int(rect.w), f"rect.w={rect.w} is not integer"
            assert rect.h == int(rect.h), f"rect.h={rect.h} is not integer"
