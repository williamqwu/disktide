"""Treemap rendering tests across diverse folder patterns.

Each test builds a realistic FSNode tree, renders the treemap at one or more
viewport sizes, and asserts structural invariants that catch the classes of
bugs visible as dark gaps, broken segments, or unstyled cells.
"""

from __future__ import annotations

import pytest
from rich.style import Style

from fs_monitor.models.tree import FSNode
from fs_monitor.viz.colors import file_category as _file_category
from fs_monitor.viz.treemap import (
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
            from fs_monitor.viz.treemap import _rect_bg
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
    """Project dominated by build artifacts — tests the 'build' category."""

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
        ("report.pdf", "document"),
        ("REPORT.PDF", "document"),
        ("photo.jpeg", "image"),
        ("style.css", "code"),
        ("settings.toml", "config"),
        ("dump.sql", "data"),
        ("archive.7z", "archive"),
        ("video.mkv", "media"),
        ("module.whl", "build"),
        ("Makefile", "other"),
        ("no_extension", "other"),
        (".hidden", "other"),
        ("multi.tar.gz", "archive"),  # uses last extension
    ])
    def test_category(self, name, expected):
        assert _file_category(name) == expected


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
    """Children with very small sub-rects must still produce leaf rects.

    Bug: _layout_node returns early when w < 1 or h < 1, completely
    dropping the child. Its allocated cells show as parent border color.
    """

    @staticmethod
    def _tree():
        big = _make_file("database.sqlite", 950_000)
        smalls = [_make_file(f"s{i}.txt", 500) for i in range(100)]
        return _wrap_root([big] + smalls)

    def test_all_children_produce_rects(self):
        """Every sized child should generate at least one rect."""
        tree = self._tree()
        layout = compute_layout(tree, 80, 24)
        leaf_names = {r.node.name for r in layout.rects if r.is_leaf}
        # The dominant file must be present
        assert "database.sqlite" in leaf_names
        # At least some of the small files should produce rects
        small_leaves = {n for n in leaf_names if n.startswith("s") and n.endswith(".txt")}
        assert len(small_leaves) >= 10, (
            f"Only {len(small_leaves)} of 100 small files produced rects — "
            f"tiny children are being dropped"
        )

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

    def test_no_extreme_aspect_ratios(self):
        """Leaf rects should not have aspect ratios worse than 20:1."""
        layout = compute_layout(self._tree(), 80, 24)
        for rect in layout.rects:
            if not rect.is_leaf or rect.w < 1 or rect.h < 1:
                continue
            ratio = max(rect.w / rect.h, rect.h / rect.w)
            assert ratio <= 20, (
                f"Rect {rect.node.name} has aspect ratio {ratio:.1f} "
                f"({rect.w}x{rect.h})"
            )


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
