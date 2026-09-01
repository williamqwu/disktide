"""Tests for visualization modules."""

import math
import struct

import pytest
from disktide.glyphs import visible_width
from disktide.models.tree import FSNode
from disktide.viz import cellgeom
from disktide.viz.colors import set_color_scheme
from disktide.viz.treemap import compute_layout, render_line
from disktide.viz.sunburst import (
    _arc_color,
    _parse_rgb,
    compute_sunburst,
    render_sunburst_line,
)

PANEL_BG = (30, 30, 30)


class _FakeTTY:
    """Stand-in for a stream whose descriptor a monkeypatched ioctl reads."""

    def fileno(self) -> int:
        return 1


def _rendered_cells(layout):
    """Flatten the sunburst render to {(x, y): (char, style)}."""
    grid = {}
    for y in range(layout.char_height):
        x = 0
        for seg in render_sunburst_line(layout, y):
            for ch in seg.text:
                grid[(x, y)] = (ch, seg.style)
                x += 1
    return grid


def _label_cells(layout):
    """Cells claimed by a label, which keeps its own chip background."""
    claimed = set()
    for label in layout.labels:
        for i in range(-1, len(label.text) + 1):
            claimed.add((label.char_x + i, label.char_y))
    return claimed


def _cell_radius_range(layout, char_x, char_y):
    """Exact (nearest, farthest) distance from the disc centre to a cell.

    Everything is in units — one unit is a cell *width*, and a cell is
    ``cell_aspect`` units tall — which is the space the renderer works in.
    """
    aspect = layout.cell_aspect
    x0, x1 = float(char_x), float(char_x + 1)
    y0, y1 = char_y * aspect, (char_y + 1) * aspect
    cx, cy = layout.center_x, layout.center_y
    nearest = math.hypot(
        min(max(cx, x0), x1) - cx,
        min(max(cy, y0), y1) - cy,
    )
    farthest = max(
        math.hypot(px - cx, py - cy)
        for px in (x0, x1)
        for py in (y0, y1)
    )
    return nearest, farthest


def _painted_bbox(layout):
    """(width, height) in cells of everything the disc pass painted."""
    xs, ys = [], []
    for y, row in enumerate(layout.rendered_cells):
        for x, (_ch, style) in enumerate(row):
            if style is not None:
                xs.append(x)
                ys.append(y)
    assert xs, "nothing was painted"
    return max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


def _style_rgbs(style):
    """Truecolor triples a style puts on screen."""
    out = []
    for color in (style.color, style.bgcolor):
        if color is not None:
            triplet = color.get_truecolor()
            out.append((triplet.red, triplet.green, triplet.blue))
    return out


def _chain_tree():
    """root -> a -> b -> c, each 100% of its parent: every ring is a full disc."""
    c = FSNode(name="c", path="/r/a/b/c", size=1000, own_size=1000, is_dir=False, depth=3)
    b = FSNode(name="b", path="/r/a/b", size=1000, own_size=0, is_dir=True, depth=2, children=[c])
    a = FSNode(name="a", path="/r/a", size=1000, own_size=0, is_dir=True, depth=1, children=[b])
    return FSNode(name="r", path="/r", size=1000, own_size=0, is_dir=True, depth=0, children=[a])


def _split_tree():
    """Two sibling directories at exactly 50% each, so ring 1 has one seam."""
    children = []
    for name in ("left", "right"):
        leaf = FSNode(
            name=f"{name}.bin", path=f"/r/{name}/{name}.bin",
            size=500, own_size=500, is_dir=False, depth=2,
        )
        children.append(FSNode(
            name=name, path=f"/r/{name}", size=500, own_size=0,
            is_dir=True, depth=1, children=[leaf],
        ))
    return FSNode(
        name="r", path="/r", size=1000, own_size=0,
        is_dir=True, depth=0, children=children,
    )


def make_viz_tree():
    """Create a tree suitable for visualization testing."""
    root = FSNode(
        name="root", path="/root", size=1000,
        own_size=100, is_dir=True, depth=0,
    )
    for i, (name, size) in enumerate([
        ("big", 400), ("medium", 300), ("small", 200),
    ]):
        child = FSNode(
            name=name, path=f"/root/{name}",
            size=size, own_size=size,
            is_dir=True, depth=1,
        )
        root.children.append(child)
    return root


class TestTreemap:
    def test_compute_layout(self):
        root = make_viz_tree()
        layout = compute_layout(root, 80, 40)
        assert layout.width == 80
        assert layout.height == 40
        assert len(layout.rects) > 0

    def test_compute_layout_zero_size(self):
        root = FSNode(name="empty", path="/empty", size=0, is_dir=True)
        layout = compute_layout(root, 80, 40)
        assert len(layout.rects) == 0

    def test_compute_layout_small(self):
        root = make_viz_tree()
        layout = compute_layout(root, 10, 5)
        assert layout.width == 10
        assert layout.height == 5

    def test_render_line(self):
        root = make_viz_tree()
        layout = compute_layout(root, 40, 20)
        segments = render_line(layout, 0)
        assert len(segments) > 0

    def test_render_line_out_of_bounds(self):
        root = make_viz_tree()
        layout = compute_layout(root, 40, 20)
        segments = render_line(layout, -1)
        assert len(segments) == 0  # Empty for out-of-bounds

    def test_rect_at(self):
        root = make_viz_tree()
        layout = compute_layout(root, 40, 20)
        # Should find a rect somewhere in the layout
        found = False
        for y in range(20):
            for x in range(40):
                if layout.rect_at(x, y) is not None:
                    found = True
                    break
            if found:
                break
        assert found


    def test_area_proportionality(self):
        """Leaf areas should be roughly proportional to data sizes."""
        root = FSNode(
            name="root", path="/root", size=1000,
            own_size=0, is_dir=True, depth=0,
            children=[
                FSNode(name="big", path="/root/big", size=600, is_dir=False, depth=1),
                FSNode(name="med", path="/root/med", size=300, is_dir=False, depth=1),
                FSNode(name="sml", path="/root/sml", size=100, is_dir=False, depth=1),
            ],
        )
        layout = compute_layout(root, 80, 24)
        areas = {}
        for rect in layout.rects:
            if rect.is_leaf:
                areas[rect.node.name] = rect.w * rect.h
        assert "big" in areas and "med" in areas and "sml" in areas
        # big should have more area than med, med more than sml
        assert areas["big"] > areas["med"], (
            f"big area {areas['big']} should exceed med area {areas['med']}"
        )
        assert areas["med"] > areas["sml"], (
            f"med area {areas['med']} should exceed sml area {areas['sml']}"
        )


class TestSunburst:
    def test_compute_sunburst(self):
        root = make_viz_tree()
        layout = compute_sunburst(root, 60, 25)
        assert layout.char_width == 60
        assert layout.char_height == 25
        assert len(layout.arcs) > 0

    def test_compute_sunburst_zero_size(self):
        root = FSNode(name="empty", path="/empty", size=0, is_dir=True)
        layout = compute_sunburst(root, 40, 20)
        assert len(layout.arcs) == 0

    def test_sunburst_renders_centered(self):
        """Sunburst should paint cells in the center rows."""
        root = make_viz_tree()
        layout = compute_sunburst(root, 60, 25)
        # A covered cell is usually a space over a background, so "content"
        # has to be read from the styles rather than from the text.
        non_empty_rows = [
            y
            for y, row in enumerate(layout.rendered_cells)
            if any(style is not None for _ch, style in row)
        ]
        assert len(non_empty_rows) > 0, "Sunburst should have visible content"
        # Content should be centered vertically (roughly middle half)
        center = layout.char_height // 2
        assert any(abs(y - center) <= center // 2 for y in non_empty_rows), \
            f"Content should be near center row {center}, got rows {non_empty_rows}"

    def test_sunburst_arcs_per_child(self):
        """Should have arcs for root + each child."""
        root = make_viz_tree()
        layout = compute_sunburst(root, 60, 25)
        # Root arc + 3 children = at least 4 arcs
        assert len(layout.arcs) >= 4

    def test_render_sunburst_line_out_of_bounds(self):
        root = make_viz_tree()
        layout = compute_sunburst(root, 40, 20)
        segments = render_sunburst_line(layout, -1)
        assert len(segments) == 0  # Empty for out-of-bounds
        segments = render_sunburst_line(layout, 999)
        assert len(segments) == 0

    def test_rendered_cells_cached(self):
        """rendered_cells property should return same object on repeated calls."""
        root = make_viz_tree()
        layout = compute_sunburst(root, 60, 25)
        rows1 = layout.rendered_cells
        rows2 = layout.rendered_cells
        assert rows1 is rows2

    def test_sunburst_fills_width(self):
        """Sunburst should use more than 50% of the widget width with deep data."""
        # Build a tree deep enough to fill the chart (5 depth levels)
        root = FSNode(
            name="root", path="/root", size=1000, own_size=0, is_dir=True, depth=0,
        )
        d1 = FSNode(name="src", path="/root/src", size=600, own_size=0, is_dir=True, depth=1)
        d2 = FSNode(name="lib", path="/root/src/lib", size=400, own_size=0, is_dir=True, depth=2)
        d3 = FSNode(name="core", path="/root/src/lib/core", size=300, own_size=0, is_dir=True, depth=3)
        f1 = FSNode(name="main.py", path="/root/src/lib/core/main.py", size=300, own_size=300, is_dir=False, depth=4)
        f2 = FSNode(name="util.py", path="/root/src/lib/util.py", size=100, own_size=100, is_dir=False, depth=3)
        f3 = FSNode(name="app.py", path="/root/src/app.py", size=200, own_size=200, is_dir=False, depth=2)
        f4 = FSNode(name="readme.md", path="/root/readme.md", size=400, own_size=400, is_dir=False, depth=1)
        root.children = [d1, f4]
        d1.children = [d2, f3]
        d2.children = [d3, f2]
        d3.children = [f1]

        layout = compute_sunburst(root, 60, 25)
        cols_with_content = set()
        for row in layout.rendered_cells:
            for x, (_ch, style) in enumerate(row):
                if style is not None:
                    cols_with_content.add(x)
        assert len(cols_with_content) > layout.char_width // 2, \
            f"Chart should use >50% width, only {len(cols_with_content)}/{layout.char_width}"

    def test_sunburst_labels_present(self):
        """Large arcs should have text labels."""
        root = make_viz_tree()
        layout = compute_sunburst(root, 60, 25)
        assert len(layout.labels) > 0, "Sunburst should have labels"
        label_texts = [lb.text for lb in layout.labels]
        assert any("root" in t for t in label_texts), "Should have root label"



class TestSunburstFill:
    """The disc is a circle, filled flat, with anti-aliased edges.

    The braille renderer this replaces could only turn a dot on or off, so
    it had to assume a cell was exactly 2:1 (the only ratio at which a
    braille dot is square) and it drew every partially covered cell as
    stippling.  On a taller cell the disc came out a vertical ellipse, and
    the dotted rims — especially the walls of the empty wedges that cut
    through the disc wherever a leaf has no children — read as noise beside
    the solid interiors.

    Every layout here asks for `shape="disc"` by name.  The configured
    default is `tiles`, whose whole claim is that it paints no partially
    covered cell at all — so a rim-anti-aliasing test read against it is
    not a weaker test, it is a test of something else.
    """

    @pytest.fixture(autouse=True)
    def _use_default_scheme(self):
        set_color_scheme("disktide")
        yield
        set_color_scheme("disktide")

    @pytest.mark.parametrize("aspect", [2.0, 2.6])
    def test_disc_is_round_at_any_cell_aspect(self, aspect):
        """The painted bbox is `aspect` times wider than tall, in cells.

        A cell that is `aspect` units tall means a circle covers `aspect`
        times as many columns as rows.  Fixing that ratio at 2.0 — which is
        what assuming square braille dots amounts to — stretches the disc
        vertically by aspect/2 on any other font.
        """
        layout = compute_sunburst(
            _chain_tree(), 100, 46, max_depth=4,
            cell_aspect=aspect, panel_bg=PANEL_BG, shape="disc",
        )
        width, height = _painted_bbox(layout)
        ratio = width / height
        assert ratio == pytest.approx(aspect, rel=0.15), (
            f"disc bbox {width}x{height} is ratio {ratio:.2f}; at cell "
            f"aspect {aspect} a circle must come out {aspect}"
        )

    def test_interior_cells_are_flat_fills(self):
        """Cells wholly inside one arc are ' ' over exactly that arc's color.

        Uniform subsamples must average back to their own input, or the
        interior of every ring would drift a shade off its own color.
        """
        layout = compute_sunburst(
            _chain_tree(), 100, 46, max_depth=4,
            cell_aspect=2.0, panel_bg=PANEL_BG, shape="disc",
        )
        root_arc = next(arc for arc in layout.arcs if arc.depth == 0)
        expected = _parse_rgb(_arc_color(root_arc))
        cells = layout.rendered_cells

        interior = 0
        for y, row in enumerate(cells):
            for x, (ch, style) in enumerate(row):
                nearest, farthest = _cell_radius_range(layout, x, y)
                # Clear of the centre hole and of the ring separator that
                # rides the outer edge of every ring but the last.
                if nearest < layout.hole_radius + 1 or farthest > root_arc.r_outer - 1:
                    continue
                interior += 1
                assert ch == " ", (
                    f"interior cell ({x},{y}) rendered {ch!r} — a flat fill "
                    f"has nothing to draw but its background"
                )
                assert style is not None and style.bgcolor is not None, (
                    f"interior cell ({x},{y}) has no background — it would "
                    f"render as a hole"
                )
                triplet = style.bgcolor.get_truecolor()
                assert (triplet.red, triplet.green, triplet.blue) == expected, (
                    f"interior cell ({x},{y}) is {triplet} not {expected} — "
                    f"blending shifted a uniform sample"
                )
        assert interior > 60, f"only {interior} interior cells sampled"

    def test_rim_is_antialiased_against_the_panel(self):
        """The rim resolves as a blend, and nothing is painted past it."""
        layout = compute_sunburst(
            _chain_tree(), 100, 46, max_depth=4,
            cell_aspect=2.0, panel_bg=PANEL_BG, shape="disc",
        )
        reach = max(arc.r_outer for arc in layout.arcs)
        outermost = max(layout.arcs, key=lambda arc: arc.depth)
        arc_rgb = _parse_rgb(_arc_color(outermost))
        cells = layout.rendered_cells

        blended = []
        for y, row in enumerate(cells):
            for x, (_ch, style) in enumerate(row):
                nearest, _farthest = _cell_radius_range(layout, x, y)
                assert style is None or nearest <= layout.radius + 1, (
                    f"cell ({x},{y}) sits beyond the disc but is painted"
                )
                if style is None or nearest < reach - 1.5:
                    continue
                for rgb in _style_rgbs(style):
                    if all(
                        min(arc_rgb[i], PANEL_BG[i])
                        < rgb[i]
                        < max(arc_rgb[i], PANEL_BG[i])
                        for i in range(3)
                    ):
                        blended.append((x, y, rgb))
        assert blended, (
            f"no rim cell blends between the arc {arc_rgb} and the panel "
            f"{PANEL_BG}: the edge is still quantized"
        )

    def test_rim_uses_half_blocks(self):
        """Replaces test_sunburst_has_braille_chars, which died with braille.

        A cell the rim crosses horizontally has one half covered and one
        half not, which is exactly what U+2580/U+2584 express.
        """
        layout = compute_sunburst(
            _chain_tree(), 100, 46, max_depth=4,
            cell_aspect=2.0, panel_bg=PANEL_BG, shape="disc",
        )
        reach = max(arc.r_outer for arc in layout.arcs)
        halves = [
            (x, y)
            for y, row in enumerate(layout.rendered_cells)
            for x, (ch, _style) in enumerate(row)
            if ch in ("▀", "▄")
            and _cell_radius_range(layout, x, y)[1] > reach - 2
        ]
        assert halves, "the rim should be drawn with half-block cells"

    def test_seams_divide_siblings_and_rings(self):
        """Separators darken the boundary between neighbours.

        Sibling directories at one depth are otherwise the same color, and
        rings run into each other, so without these the chart is one blob
        with no readable structure.
        """
        layout = compute_sunburst(
            _split_tree(), 100, 46, max_depth=4,
            cell_aspect=2.0, panel_bg=PANEL_BG, shape="disc",
        )
        ring1 = [arc for arc in layout.arcs if arc.depth == 1]
        assert len(ring1) == 2
        root_arc = next(arc for arc in layout.arcs if arc.depth == 0)
        sibling_rgbs = [_parse_rgb(_arc_color(arc)) for arc in ring1]
        root_rgb = _parse_rgb(_arc_color(root_arc))

        angular, radial = [], []
        for hy, row in enumerate(layout.frame):
            for hx, rgb in enumerate(row):
                if rgb is None:
                    continue
                ux = hx + 0.5
                uy = (hy + 0.5) * layout.cell_aspect / 2.0
                dx = ux - layout.center_x
                dy = uy - layout.center_y
                radius = math.hypot(dx, dy)
                theta = math.atan2(dy, dx) % (2 * math.pi)
                on_ring1 = ring1[0].r_inner + 1 < radius < ring1[0].r_outer - 1
                to_seam = min(
                    abs(theta), abs(theta - math.pi), abs(theta - 2 * math.pi)
                )
                if on_ring1 and to_seam * radius < 1.2:
                    if sum(rgb) < min(sum(c) for c in sibling_rgbs):
                        angular.append((hx, hy, rgb))
                if abs(radius - root_arc.r_outer) < 1.0:
                    if sum(rgb) < min(
                        sum(root_rgb), min(sum(c) for c in sibling_rgbs)
                    ):
                        radial.append((hx, hy, rgb))

        assert angular, (
            f"no cell on the boundary between siblings {sibling_rgbs} is "
            f"darker than both — the two arcs merge into one wedge"
        )
        assert radial, (
            f"no cell on the ring-0/ring-1 boundary is darker than both "
            f"{root_rgb} and {sibling_rgbs} — the rings merge"
        )

    def test_dir_siblings_alternate_in_luminance(self):
        """Even where a seam cannot fit, siblings still differ."""
        layout = compute_sunburst(
            _split_tree(), 100, 46, max_depth=4,
            cell_aspect=2.0, panel_bg=PANEL_BG, shape="disc",
        )
        ring1 = [arc for arc in layout.arcs if arc.depth == 1]
        first, second = (_parse_rgb(_arc_color(arc)) for arc in ring1)
        assert first != second, (
            "sibling directories at one depth share a color, so a run of "
            "them reads as a single undivided blob"
        )

    def test_arc_label_sits_on_its_arc_at_a_wide_cell(self):
        """Label placement goes through the same unit transform as the disc."""
        aspect = 2.6
        layout = compute_sunburst(
            make_viz_tree(), 100, 46, max_depth=4,
            cell_aspect=aspect, panel_bg=PANEL_BG, shape="disc",
        )
        wide = [
            arc
            for arc in layout.arcs
            if arc.depth == 1 and arc.angle_span > math.radians(30)
        ]
        assert wide, "fixture should produce labellable depth-1 arcs"

        by_name = {label.text: label for label in layout.labels}
        checked = 0
        for arc in wide:
            label = by_name.get(arc.node.name)
            if label is None:  # lost a collision to a neighbour
                continue
            checked += 1
            mid_r = (arc.r_inner + arc.r_outer) / 2
            want_y = (
                layout.char_height / 2
                + mid_r * math.sin(arc.angle_mid) / aspect
            )
            assert abs(label.char_y - want_y) <= 1, (
                f"label for {arc.node.name!r} is on row {label.char_y}, but "
                f"its arc runs through row {want_y:.1f}"
            )
            # And the row really does land inside the arc's ring.
            centre_x = label.char_x + visible_width(label.text) / 2
            dx = centre_x - layout.center_x
            dy = (label.char_y + 0.5) * aspect - layout.center_y
            radius = math.hypot(dx, dy)
            assert arc.r_inner - 1 <= radius <= arc.r_outer + 1, (
                f"label for {arc.node.name!r} sits at radius {radius:.1f}, "
                f"outside its ring [{arc.r_inner:.1f}, {arc.r_outer:.1f}]"
            )
        assert checked >= 2, f"only {checked} labels checked"

    def test_sub_threshold_arcs_leave_no_cracks(self):
        """A sliver too thin to subdivide still owns its slice of the ring.

        Arcs spanning under half a degree used to be dropped before they
        were recorded, so nothing claimed their angle and the rasterizer
        left a hairline of unpainted dots running through the ring.
        """
        big = FSNode(
            name="big", path="/r/big", size=990, own_size=990,
            is_dir=False, depth=1,
        )
        crumbs = [
            FSNode(
                name=f"c{i:02d}", path=f"/r/c{i:02d}", size=1, own_size=1,
                is_dir=False, depth=1,
            )
            for i in range(60)
        ]
        root = FSNode(
            name="r", path="/r", size=990 + len(crumbs), own_size=0,
            is_dir=True, depth=0, children=[big] + crumbs,
        )
        layout = compute_sunburst(
            root, 100, 46, max_depth=4, cell_aspect=2.0, panel_bg=PANEL_BG, shape="disc",
        )

        spans = [arc.angle_span for arc in layout.arcs if arc.depth == 1]
        assert len(spans) == 61, f"expected every child to be recorded, got {len(spans)}"
        assert min(spans) < math.radians(0.5), "fixture should produce sub-0.5deg arcs"

        ring = next(arc for arc in layout.arcs if arc.depth == 1)
        cells = layout.rendered_cells

        checked = 0
        for y, row in enumerate(cells):
            for x, (ch, style) in enumerate(row):
                nearest, farthest = _cell_radius_range(layout, x, y)
                if nearest < ring.r_inner + 1 or farthest > ring.r_outer - 1:
                    continue
                checked += 1
                assert ch == " " and style is not None and style.bgcolor is not None, (
                    f"ring-1 cell ({x},{y}) rendered {ch!r} — an unowned "
                    f"angular sliver punched a pinhole through the ring"
                )
        assert checked > 20, f"only {checked} ring-1 cells sampled"


class TestCellGeometry:
    """Measuring the terminal's cell instead of assuming it is 2:1."""

    @pytest.fixture(autouse=True)
    def _no_env(self, monkeypatch):
        monkeypatch.delenv(cellgeom.ASPECT_ENV_VAR, raising=False)

    @staticmethod
    def _winsize(rows, cols, xpixel, ypixel):
        return struct.pack("HHHH", rows, cols, xpixel, ypixel)

    def _fake_tty(self, monkeypatch, packed):
        monkeypatch.setattr(
            cellgeom, "_candidate_streams", lambda: iter([_FakeTTY()])
        )
        monkeypatch.setattr(
            cellgeom.fcntl, "ioctl", lambda fd, request, buf: packed
        )

    def test_env_override_wins(self, monkeypatch):
        self._fake_tty(monkeypatch, self._winsize(53, 299, 2093, 901))
        monkeypatch.setenv(cellgeom.ASPECT_ENV_VAR, "3.1")
        assert cellgeom.detect_cell_aspect() == pytest.approx(3.1)

    def test_env_override_is_clamped(self, monkeypatch):
        monkeypatch.setenv(cellgeom.ASPECT_ENV_VAR, "9")
        assert cellgeom.detect_cell_aspect() == cellgeom.MAX_CELL_ASPECT
        monkeypatch.setenv(cellgeom.ASPECT_ENV_VAR, "0.2")
        assert cellgeom.detect_cell_aspect() == cellgeom.MIN_CELL_ASPECT

    def test_unparseable_env_falls_back(self, monkeypatch):
        monkeypatch.setenv(cellgeom.ASPECT_ENV_VAR, "very tall")
        assert cellgeom.detect_cell_aspect() == cellgeom.DEFAULT_CELL_ASPECT

    def test_reads_pixel_size_from_ioctl(self, monkeypatch):
        # 299x53 cells over 2093x901 px: a 7x17 cell, aspect 17/7.
        self._fake_tty(monkeypatch, self._winsize(53, 299, 2093, 901))
        assert cellgeom.detect_cell_aspect() == pytest.approx(17 / 7)

    def test_ioctl_result_is_clamped(self, monkeypatch):
        self._fake_tty(monkeypatch, self._winsize(10, 10, 100, 10000))
        assert cellgeom.detect_cell_aspect() == cellgeom.MAX_CELL_ASPECT

    def test_terminal_without_pixel_size(self, monkeypatch):
        self._fake_tty(monkeypatch, self._winsize(53, 299, 0, 0))
        assert cellgeom.detect_cell_aspect() == cellgeom.DEFAULT_CELL_ASPECT

    def test_ioctl_failure_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            cellgeom, "_candidate_streams", lambda: iter([_FakeTTY()])
        )

        def _boom(fd, request, buf):
            raise OSError(25, "Inappropriate ioctl for device")

        monkeypatch.setattr(cellgeom.fcntl, "ioctl", _boom)
        assert cellgeom.detect_cell_aspect() == cellgeom.DEFAULT_CELL_ASPECT

    def test_any_exception_falls_back(self, monkeypatch):
        def _boom():
            raise RuntimeError("no streams here")

        monkeypatch.setattr(cellgeom, "_candidate_streams", _boom)
        assert cellgeom.detect_cell_aspect() == cellgeom.DEFAULT_CELL_ASPECT

    def test_treemap_squareness_follows_the_cell(self):
        """A 'square' rect is square on screen only at the real aspect."""
        root = FSNode(
            name="root", path="/root", size=1000,
            own_size=0, is_dir=True, depth=0,
            children=[
                FSNode(name=f"f{i}.py", path=f"/root/f{i}.py", size=size,
                       own_size=size, is_dir=False, depth=1)
                for i, size in enumerate((600, 300, 100))
            ],
        )
        wide = compute_layout(root, 60, 24, cell_aspect=3.0)
        narrow = compute_layout(root, 60, 24, cell_aspect=1.5)
        default = compute_layout(root, 60, 24)
        assert compute_layout(root, 60, 24, cell_aspect=None).rects == default.rects
        shapes = {
            name: [(r.w, r.h) for r in layout.rects if r.is_leaf]
            for name, layout in
            (("wide", wide), ("narrow", narrow), ("default", default))
        }
        assert shapes["wide"] != shapes["narrow"], (
            "the treemap ignored cell_aspect: rectangles that are square at "
            "3.0 cannot also be square at 1.5"
        )
