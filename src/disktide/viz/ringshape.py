"""Where a ring chart's rings are, and how a ring is cut into siblings.

A sunburst is written against two functions of a point's offset from the
chart centre -- how far out it is, and how far around -- and every ring
boundary, separator, hit test and label placement is expressed in those
two numbers.  Three shapes answer them differently:

``disc``   Round rings.  `hypot` out, `atan2` around.
``fill``   Rings as rectangles stretched to the pane's own edges:
           Chebyshev distance out, position along the perimeter around.
           Segment boundaries are still rays from the centre.
``tiles``  The same rectangular rings, cut by straight lines instead.

Why bother: a terminal cell is a rectangle, and a circle built out of
rectangles is an approximation at every point of its rim.  Anti-aliasing
that rim is a way of admitting an edge falls between two cells rather
than on one -- the disc's silhouette, its centre hole and all four of its
ring boundaries are soft for exactly that reason.  A rectangular ring
puts those edges on cell edges, where there is nothing to approximate.

**What ``tiles`` fixes.**  Under ``fill`` the ring boundaries are exact
and the *separators between siblings* are not: they stay rays from the
centre, and a ray is a diagonal everywhere except at four points, so the
picture keeps a mosaic of stepped diagonals over crisp frames.  A ray is
unavoidable while "how far around" is a function of direction alone.
``tiles`` makes it a function of the band as well: within one ring, a
point's position around the loop is its offset *along the face it is on*
-- x on the top and bottom faces, y on the left and right -- measured
against that band's own midline rectangle.  Every point sharing an x on
the top face then shares an angle, so the cut between two siblings is a
vertical line; on the side faces it is a horizontal one.  Nothing in the
picture is diagonal any more.

The trade is that the radial reading goes with it.  A child's cut and its
parent's are at the same fraction of two different loops, so they land at
the same place along a face but step where a loop turns a corner: the
chart reads as nested frames of tiles rather than as rays.

**Area still equals value, exactly.**  Under ``fill`` that holds because
a rectangle's centre is equidistant from all four edge *lines*, so a
wedge's area is proportional to the perimeter it spans.  Under ``tiles``
the loop coordinate is cumulative *area* rather than length -- a face of
length L carries L times the band's thickness across that face -- and
summing it around the loop gives 4(a*wy + b*wx) for a midline rectangle
(a, b) and ring widths (wx, wy), which is the band's own area to the
last term.  Corners cost nothing: the two ways of counting the corner
square cancel.

Angles keep the disc's convention -- zero at the middle of the top edge
for ``tiles``, at the middle of the right edge for the others,
increasing clockwise on screen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


#: Every shape `ui.ring_shape` may name, in the order the explorer's
#: cycle key steps through them.  The order is the order they were built
#: in, which is also roughly the order of how much of the grid they land
#: on; the default is picked below, not by position.
RING_SHAPES: tuple[str, ...] = ("disc", "fill", "tiles")
#: `tiles` is the default: it is the only one of the three that draws no
#: partially covered cell at any cell aspect, so it needs no aspect
#: calibration to look right and it is sharp on a terminal that reports
#: nothing about its own font.  The trade is real and is the reason the
#: other two stay reachable -- nesting frames of tiles gives up the disc's
#: radial parent/child alignment, so it reads less like a sunburst.
DEFAULT_RING_SHAPE = "tiles"

#: Unpainted centre of a round or stretched-rectangle chart, in units.
#: `tiles` sizes its own hole -- see `geometry_for`.
DEFAULT_HOLE_RADIUS = 2.0

_TWO_PI = 2.0 * math.pi
# One eighth of a turn is one half-edge of the square, which is the unit
# the perimeter walk in `BoxGeometry` counts in.
_EIGHTH = math.pi / 4.0
# The unit square's perimeter is 8 and a turn is 2*pi, so one radian is
# 4/pi units of edge -- against exactly 1 for the unit circle.  Seam
# widths are given in units of edge, so they go through this to stay
# hairlines of the same width in either shape.
_EDGE_PER_RADIAN = 4.0 / math.pi


def resolve_ring_shape(name: str | None) -> str:
    """The shape *name* means, falling back rather than raising.

    Same contract as `themes.resolve_theme`, and for the same reason: an
    unknown value is a config file someone hand-edited or one written by
    a newer build, and neither is a reason to refuse to start.
    """
    return name if name in RING_SHAPES else DEFAULT_RING_SHAPE


@dataclass(frozen=True)
class DiscGeometry:
    """Round rings: the shape the chart has always had.

    The arithmetic here is what the rasterizer used to do inline, moved
    behind a name so other shapes can exist.  It is deliberately written
    the same way -- `dx * dx + dy * dy` under a square root, not
    `math.hypot` -- so that no cell changes colour on a layout that asked
    for a disc.

    `band` is the radius of the ring a sample landed in, which only a
    per-band parameterisation needs; the two shapes that answer "how far
    around" from direction alone ignore it.
    """

    name: str = "disc"
    #: Seams are anti-aliased hairlines here; only a shape whose edges
    #: already land on the cell grid can afford to quantise them.
    crisp_seams: bool = False

    def radius(self, dx: float, dy: float) -> float:
        return math.sqrt(dx * dx + dy * dy)

    def snap_angle(self, theta: float, band: float) -> float:
        return theta

    def angle(self, dx: float, dy: float, band: float) -> float:
        theta = math.atan2(dy, dx)
        return theta + _TWO_PI if theta < 0.0 else theta

    def offset(self, theta: float, radius: float) -> tuple[float, float]:
        return (radius * math.cos(theta), radius * math.sin(theta))

    def edge_per_radian(self, theta: float, radius: float, band: float) -> float:
        return radius

    def row_half_width(self, distance: float, reach: float) -> float:
        """Half-width in units covered at `distance` above or below centre.

        Negative when the row misses the chart entirely, which is the
        rasterizer's signal to skip it without sampling anything.
        """
        if distance >= reach:
            return -1.0
        return math.sqrt(reach * reach - distance * distance)


@dataclass(frozen=True)
class BoxGeometry:
    """Rectangular rings cut by rays, stretched to fill the pane.

    `scale_x` and `scale_y` are how far the iso-radius rectangle reaches
    along each axis per unit of radius.  The point is mapped into the
    unstretched square first, which is why every method starts by
    dividing the offset through -- and why the area proportionality in
    the module docstring holds for a stretched rectangle as well.
    """

    scale_x: float = 1.0
    scale_y: float = 1.0
    name: str = "fill"
    crisp_seams: bool = False
    # Reciprocals, because the rasterizer calls `radius` and `angle` four
    # times per half-cell and a multiply is cheaper than a divide.
    _inv_x: float = field(init=False, repr=False, compare=False)
    _inv_y: float = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_inv_x", 1.0 / self.scale_x)
        object.__setattr__(self, "_inv_y", 1.0 / self.scale_y)

    def radius(self, dx: float, dy: float) -> float:
        u = dx * self._inv_x
        v = dy * self._inv_y
        if u < 0.0:
            u = -u
        if v < 0.0:
            v = -v
        return u if u > v else v

    def snap_angle(self, theta: float, band: float) -> float:
        return theta

    def angle(self, dx: float, dy: float, band: float) -> float:
        """Where the point sits on the perimeter, as an angle in [0, 2pi).

        Counted in eighths of a turn from the middle of the right edge,
        clockwise: the right edge is the half-open pair [0, 1) and [7, 8),
        the bottom edge [1, 3), the left edge [3, 5) and the top [5, 7).
        Inside one edge the count is linear in the offset along it, which
        is what makes equal angles equal lengths of perimeter.
        """
        u = dx * self._inv_x
        v = dy * self._inv_y
        au = -u if u < 0.0 else u
        av = -v if v < 0.0 else v
        if u >= av:
            if u <= 0.0:
                # Dead centre: no edge to be on.  Only reachable from a
                # hit test on the hole, which discards the angle anyway.
                return 0.0
            eighths = v / u
            if eighths < 0.0:
                eighths += 8.0
        elif v >= au:
            eighths = 2.0 - u / v
        elif -u >= av:
            eighths = 4.0 + v / u
        else:
            eighths = 6.0 - u / v
        return eighths * _EIGHTH

    def offset(self, theta: float, radius: float) -> tuple[float, float]:
        """Inverse of `angle`, at a given radius. Used to place labels."""
        eighths = theta / _EIGHTH
        if eighths < 1.0:
            u, v = 1.0, eighths
        elif eighths < 3.0:
            u, v = 2.0 - eighths, 1.0
        elif eighths < 5.0:
            u, v = -1.0, 4.0 - eighths
        elif eighths < 7.0:
            u, v = eighths - 6.0, -1.0
        else:
            u, v = 1.0, eighths - 8.0
        return (u * radius * self.scale_x, v * radius * self.scale_y)

    def edge_per_radian(self, theta: float, radius: float, band: float) -> float:
        """Units of perimeter one radian buys at `theta`.

        The two pairs of edges are stretched by different amounts -- the
        left and right edges run in y, the top and bottom in x -- so a
        seam asks which edge it is on before deciding how wide a hairline
        is.
        """
        eighths = theta / _EIGHTH
        vertical = eighths < 1.0 or eighths >= 7.0 or 3.0 <= eighths < 5.0
        scale = self.scale_y if vertical else self.scale_x
        return radius * _EDGE_PER_RADIAN * scale

    def row_half_width(self, distance: float, reach: float) -> float:
        """Half-width in units covered at `distance` above or below centre.

        A rectangle is as wide at its top as at its middle, so this is
        flat right up to the edge and then gone.
        """
        if distance >= reach * self.scale_y:
            return -1.0
        return reach * self.scale_x


@dataclass(frozen=True)
class TileGeometry:
    """Rectangular rings cut by straight lines: no diagonal anywhere.

    Contour *m* is the rectangle of half-extents ``(m * unit_x,
    m * unit_y)``.  `unit_x` is a whole number of columns and `unit_y` a
    whole number of half-rows -- the framebuffer's two quanta -- so every
    ring boundary, the hole (contour 1) and the outer silhouette land
    exactly on a cell edge in both axes, at any cell aspect.  That is the
    difference between this and a shape whose single ring width has to
    snap to two grids at once.

    Band *k* is the annulus between contours k and k+1, and it is split
    into faces along its **inner** rectangle: a point belongs to a side
    face while ``|dy| <= k * unit_y`` and to the top or bottom face
    otherwise.  Each band corner therefore goes whole to the face above
    or below it rather than being halved along a diagonal, and the two
    faces carry constant thickness -- `unit_x` across a side, `unit_y`
    across the top -- so cumulative *area* around the loop is
    ``dx * unit_y`` and ``dy * unit_x``, and the four faces sum to
    ``4 * (k + 1) * unit_x * unit_y + 4 * k * unit_y * unit_x``, which is
    the band's own area exactly.  Both reference extents are whole cells,
    which is what lets a cut be rounded onto the grid without moving off
    the face it was on.

    Radii are reported in units so the rest of the chart keeps its
    vocabulary: `step` is one contour in those units (the mean of the two
    axes), and a radius of ``m * step`` is contour *m*.
    """

    unit_x: float
    unit_y: float
    step: float
    #: Half a character cell, in units: the framebuffer's vertical
    #: quantum, and the grid a y coordinate is rounded to.
    half_row: float = 1.0
    name: str = "tiles"
    #: Everything this shape draws lands on a cell edge, so its seams are
    #: whole cells rather than hairlines mixed into their neighbours -- an
    #: anti-aliased seam would be the only soft thing left in a picture
    #: whose whole point is that nothing is.
    crisp_seams: bool = True
    _inv_x: float = field(init=False, repr=False, compare=False)
    _inv_y: float = field(init=False, repr=False, compare=False)
    _inv_step: float = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_inv_x", 1.0 / self.unit_x)
        object.__setattr__(self, "_inv_y", 1.0 / self.unit_y)
        object.__setattr__(self, "_inv_step", 1.0 / self.step)

    def radius(self, dx: float, dy: float) -> float:
        u = (-dx if dx < 0.0 else dx) * self._inv_x
        v = (-dy if dy < 0.0 else dy) * self._inv_y
        return (u if u > v else v) * self.step

    def _faces(self, band: float) -> tuple[float, float, float, float, float, float]:
        """The band's two reference extents, and the area at each corner.

        `band` is any radius inside the band; what matters is the whole
        contour below it, which is the rectangle both faces are measured
        against.
        """
        contour = int(band * self._inv_step)
        across = (contour + 1) * self.unit_x  # top and bottom reach the corners
        down = contour * self.unit_y  # the sides stop short of them
        top_right = across * self.unit_y
        bottom_right = top_right + 2.0 * down * self.unit_x
        bottom_left = bottom_right + 2.0 * across * self.unit_y
        top_left = bottom_left + 2.0 * down * self.unit_x
        return across, down, top_right, bottom_right, bottom_left, top_left

    def angle(self, dx: float, dy: float, band: float) -> float:
        """How far around its band the point is, as an angle in [0, 2pi).

        Cumulative area, clockwise from the middle of the top edge, so an
        equal angle is an equal share of the ring however the ring is
        shaped.  The offset used is the one along the face the point is
        on -- x on the top and bottom, y on the sides -- which is what
        makes a cut between two siblings a straight line.
        """
        across, down, c1, c2, c3, _c4 = self._faces(band)
        unit_x = self.unit_x
        unit_y = self.unit_y
        if (-dy if dy < 0.0 else dy) <= down:
            # A side face: the loop runs along y here, so a cut is a
            # horizontal line.
            v = down if dy > down else (-down if dy < -down else dy)
            if dx >= 0.0:
                area = c1 + (v + down) * unit_x
            else:
                area = c3 + (down - v) * unit_x
        else:
            # Top or bottom face, corners included: the loop runs along
            # x, so a cut is a vertical line.
            u = across if dx > across else (-across if dx < -across else dx)
            if dy < 0.0:
                area = u * unit_y if u >= 0.0 else _c4 + (across + u) * unit_y
            else:
                area = c2 + (across - u) * unit_y
        total = _c4 + across * unit_y
        return area / total * _TWO_PI

    def offset(self, theta: float, radius: float) -> tuple[float, float]:
        """Inverse of `angle`, on the band holding `radius`. Places labels."""
        across, down, c1, c2, c3, c4 = self._faces(radius)
        total = c4 + across * self.unit_y
        area = theta / _TWO_PI * total
        outer = down + self.unit_y  # mid-thickness is not wanted here: the
        # label sits on the face it names, which for a top face is one
        # whole band beyond the inner rectangle.
        if area < c1:
            return (area / self.unit_y, -outer)
        if area < c2:
            return (across, -down + (area - c1) / self.unit_x)
        if area < c3:
            return (across - (area - c2) / self.unit_y, outer)
        if area < c4:
            return (-across, down - (area - c3) / self.unit_x)
        return (-across + (area - c4) / self.unit_y, -outer)

    def edge_per_radian(self, theta: float, radius: float, band: float) -> float:
        """Units of ring edge one radian buys at `theta`.

        The angle counts area, so this divides back out by the band's
        thickness across the face the sample is on -- otherwise a seam
        given as a width in units would come out wider on the faces where
        the ring is thicker.
        """
        across, _down, c1, c2, c3, c4 = self._faces(band)
        total = c4 + across * self.unit_y
        area = theta / _TWO_PI * total
        on_side = c1 <= area < c2 or c3 <= area < c4
        return total / _TWO_PI / (self.unit_x if on_side else self.unit_y)

    def cell_depth(self, dx: float, dy: float, band: float) -> float:
        """One cell measured *radially* at this point, in radius units.

        A column where x is what put the point in this band and a
        half-row where y did -- the framebuffer's quantum in each
        direction, half blocks giving it half a cell vertically and
        nothing horizontally.  What a ring seam is drawn one of.

        Note this asks which coordinate the *radius* came from, not which
        face the loop coordinate runs along: the two disagree inside a
        band's corner tile, where the loop is measured in x and the radius
        can still be coming from y.  Answering with the loop's axis there
        put the seam's edge between two columns, which is the one place
        this shape cannot draw an edge.
        """
        if (-dx if dx < 0.0 else dx) * self._inv_x >= (
            (-dy if dy < 0.0 else dy) * self._inv_y
        ):
            return self.step * self._inv_x
        return self.step * self.half_row * self._inv_y

    def cell_edge(self, theta: float, band: float) -> float:
        """One cell measured *along* the face at `theta`, in units."""
        across, _down, c1, c2, c3, c4 = self._faces(band)
        total = c4 + across * self.unit_y
        area = theta / _TWO_PI * total
        on_side = c1 <= area < c2 or c3 <= area < c4
        return self.half_row if on_side else 1.0

    def snap_angle(self, theta: float, band: float) -> float:
        """Move a segment boundary onto the nearest cell edge.

        The cuts are the one thing in this shape whose position comes from
        the data rather than from the grid, so they are the one thing left
        that can split a cell between two blocks and leave the mixture on
        screen.  Rounding them costs under half a cell of value each --
        less than the terminal can show -- and buys a picture in which
        every cell is exactly one colour.  Both reference extents are
        whole cells, so rounding an absolute offset along a face keeps it
        on the face; the ring's own extent (0 and a full turn, both at the
        middle of the top edge, which the snapped centre puts on a column
        boundary) comes back untouched.
        """
        across, down, c1, c2, c3, c4 = self._faces(band)
        total = c4 + across * self.unit_y
        area = theta / _TWO_PI * total
        row = self.half_row
        if area < c1:
            x = min(across, max(0.0, round(area / self.unit_y)))
            return x * self.unit_y / total * _TWO_PI
        if area < c2:
            y = (area - c1) / self.unit_x - down
            y = min(down, max(-down, round(y / row) * row))
            return (c1 + (y + down) * self.unit_x) / total * _TWO_PI
        if area < c3:
            x = across - (area - c2) / self.unit_y
            x = min(across, max(-across, round(x)))
            return (c2 + (across - x) * self.unit_y) / total * _TWO_PI
        if area < c4:
            y = down - (area - c3) / self.unit_x
            y = min(down, max(-down, round(y / row) * row))
            return (c3 + (down - y) * self.unit_x) / total * _TWO_PI
        x = (area - c4) / self.unit_y - across
        x = min(0.0, max(-across, round(x)))
        return (c4 + (across + x) * self.unit_y) / total * _TWO_PI

    def row_half_width(self, distance: float, reach: float) -> float:
        contour = reach * self._inv_step
        if distance >= contour * self.unit_y:
            return -1.0
        return contour * self.unit_x


#: What the sunburst holds and calls.  A protocol would say the same
#: thing; three concrete classes say it in one place, and the rasterizer
#: binds the methods as locals anyway.
RingGeometry = DiscGeometry | BoxGeometry | TileGeometry


@dataclass(frozen=True)
class RingFit:
    """A shape, and the radial layout it wants in the pane it was given.

    The three numbers travel with the geometry because they are not
    independent of it: `tiles` sizes its hole and its ring width from the
    cell grid it snaps to, and a caller that computed them itself would
    have to know how.
    """

    geometry: RingGeometry
    radius: float
    hole_radius: float
    ring_width: float


def geometry_for(
    shape: str,
    char_width: int,
    char_height: int,
    cell_aspect: float,
    max_depth: int,
) -> RingFit:
    """Fit `shape` to a pane, in units (cell widths).

    For the disc that is the radius it always had -- half the shorter
    side, one unit of margin held back -- with the hole and ring width
    derived from it.  `fill` keeps the same radius and reaches each edge
    on its own axis.  `tiles` works the other way round: it picks whole
    numbers of columns and half-rows first, so that its boundaries land
    on the grid, and reports the radius those add up to.
    """
    rings = max_depth + 1
    half_row = cell_aspect / 2.0
    half_w = char_width / 2.0
    half_h = char_height * cell_aspect / 2.0

    if shape == "tiles":
        # The centre snaps to a column boundary (see `center_x`), so the
        # room on each side of it is what the contours have to divide --
        # not half the pane, which at an odd width is half a column more.
        centre = round(half_w)
        reach_x = min(centre, char_width - centre)
        steps = rings + 1  # the hole is contour 1, the silhouette the last
        unit_x = float(int(reach_x / steps))
        unit_y = float(int(half_h / (steps * half_row))) * half_row
        if unit_x < 1.0 or unit_y < half_row:
            # Too small to give every ring a whole cell on both axes.
            return RingFit(TileGeometry(1.0, half_row, 1.0), 0.0, 1.0, 1.0)
        step = (unit_x + unit_y) / 2.0
        return RingFit(
            TileGeometry(
                unit_x=unit_x, unit_y=unit_y, step=step, half_row=half_row
            ),
            radius=step * steps,
            hole_radius=step,
            ring_width=step,
        )

    if shape == "fill":
        reach_x = half_w - 1.0
        reach_y = half_h - 1.0
        radius = min(reach_x, reach_y)
        if radius <= 0.0:
            geometry: RingGeometry = BoxGeometry()
        else:
            geometry = BoxGeometry(
                scale_x=reach_x / radius, scale_y=reach_y / radius
            )
        return RingFit(
            geometry,
            radius=radius,
            hole_radius=DEFAULT_HOLE_RADIUS,
            ring_width=(radius - DEFAULT_HOLE_RADIUS) / rings,
        )

    radius = min(half_w, half_h) - 1.0
    return RingFit(
        DiscGeometry(),
        radius=radius,
        hole_radius=DEFAULT_HOLE_RADIUS,
        ring_width=(radius - DEFAULT_HOLE_RADIUS) / rings,
    )
