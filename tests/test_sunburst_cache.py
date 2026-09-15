"""The rasterizer's geometry cache, and the frames it must not change.

`_rasterize_arcs` used to ask the shape where every subsample was on every
frame: 618k function calls for one 182x62 live frame, 97 % of them
`radius`, `angle`, `edge_per_radian`, `cell_edge`, `cell_depth` and the
`_faces` those three sit on -- all functions of the offset from the centre
and the band, none of them a function of the tree.  They are computed once
per geometry now and replayed, which is 163 ms down to 48 ms on that
frame.

A cache like that fails silently: a key that misses a parameter draws a
*wrong* picture rather than raising, and a rounding difference in the
replayed arithmetic moves one cell on a rim nobody is looking at.  So the
frames are pinned.  `FRAME_PINS` was generated from the renderer as it
stood before the cache existed, across three trees, three shapes, two
sizes, three cell aspects and both depths the widget uses; the digests
must survive any change here.  Regenerating them is a deliberate act that
says "the picture is meant to be different now", and the two chart shots
in the README are what it is measured against.

The thirty-six `tiles` digests were regenerated once, when that shape's
vertical quantum went from the framebuffer's half-row to a whole row so
that it would stop drawing `▀`/`▄` a browser terminal cannot fit to a
cell.  `disc` and `fill` still draw with half blocks and their
seventy-two digests did not move, which is the check that the change was
the one it was meant to be.

The second assertion in the same test is the key's completeness: the same
matrix rendered with the cache switched off has to agree with itself
rendered with it on, in an order that makes the four-entry LRU evict and
refill throughout.
"""

from __future__ import annotations

import hashlib

import pytest

from disktide.models.tree import FSNode
from disktide.viz.colors import set_color_scheme
from disktide.viz.ringshape import RING_SHAPES
from disktide.viz.sunburst import (
    _PLAN_CACHE,
    _PLAN_CACHE_LIMIT,
    _PLAN_CACHE_MAX_SAMPLES,
    clear_sample_cache,
    compute_sunburst,
    set_sample_cache_enabled,
)


@pytest.fixture(autouse=True)
def _pinned_palette():
    """The frame carries arc colours, so the digests need a known palette."""
    set_color_scheme("disktide")
    clear_sample_cache()
    yield
    clear_sample_cache()


def _dir(name, path, depth):
    return FSNode(name=name, path=path, size=0, own_size=0, is_dir=True, depth=depth)


def _file(name, path, size, depth):
    return FSNode(name=name, path=path, size=size, own_size=size, is_dir=False,
                  depth=depth, file_count=1)


def _rollup(node):
    if not node.is_dir:
        return
    size = files = dirs = 0
    for child in node.children:
        _rollup(child)
        size += child.size
        if child.is_dir:
            files += child.file_count
            dirs += child.dir_count + 1
        else:
            files += 1
    node.size = size
    node.file_count = files
    node.dir_count = dirs


def _lopsided():
    spec = [
        ("src", [("main.py", 9000), ("util.py", 5200), ("parser.py", 3100),
                 ("cli.py", 1700)]),
        ("docs", [("guide.md", 6400), ("api.pdf", 2600), ("notes.txt", 900)]),
        ("data", [("train.parquet", 12000), ("index.db", 4400),
                  ("raw.csv", 2100), ("cache.bin", 800), ("meta.json", 400)]),
    ]
    root = _dir("root", "/root", 0)
    for name, files in spec:
        node = _dir(name, f"/root/{name}", 1)
        for fname, size in files:
            node.children.append(_file(fname, f"/root/{name}/{fname}", size, 2))
        root.children.append(node)
    _rollup(root)
    return root


def _deep():
    root = _dir("deep", "/deep", 0)
    size = 512
    for a in range(5):
        da = _dir(f"a{a}", f"/deep/a{a}", 1)
        for b in range(4):
            db = _dir(f"b{b}", f"{da.path}/b{b}", 2)
            for c in range(3):
                dc = _dir(f"c{c}", f"{db.path}/c{c}", 3)
                for f in range(3):
                    size = size * 7 % 100003 + 11
                    dc.children.append(
                        _file(f"f{f}.py", f"{dc.path}/f{f}.py", size, 4)
                    )
                db.children.append(dc)
            size = size * 5 % 100003 + 7
            db.children.append(_file("leaf.bin", f"{db.path}/leaf.bin", size, 3))
            da.children.append(db)
        root.children.append(da)
    _rollup(root)
    return root


def _single():
    root = _dir("one", "/one", 0)
    only = _dir("only", "/one/only", 1)
    only.children.append(_file("blob.bin", "/one/only/blob.bin", 4096, 2))
    root.children.append(only)
    _rollup(root)
    return root


TREES = {"lopsided": _lopsided, "deep": _deep, "single": _single}
SHAPES = ("disc", "fill", "tiles")
SIZES = ((60, 24), (96, 36))
ASPECTS = (1.5, 2.0, 2.43)
DEPTHS = (2, 4)

CASES = tuple(
    (tree, shape, w, h, aspect, depth)
    for tree in TREES
    for shape in SHAPES
    for (w, h) in SIZES
    for aspect in ASPECTS
    for depth in DEPTHS
)

_BUILT = {}


def build_tree(name):
    if name not in _BUILT:
        _BUILT[name] = TREES[name]()
    return _BUILT[name]


def frame_of(case):
    tree, shape, w, h, aspect, depth = case
    layout = compute_sunburst(
        build_tree(tree), w, h,
        max_depth=depth, metric="logical", cell_aspect=aspect,
        panel_bg=(30, 30, 30), shape=shape,
    )
    return layout.frame


def digest(case) -> str:
    return hashlib.sha256(repr(frame_of(case)).encode()).hexdigest()[:16]


FRAME_PINS = {
    ('lopsided', 'disc', 60, 24, 1.5, 2): "49521f4201b37b66",
    ('lopsided', 'disc', 60, 24, 1.5, 4): "e2b2b27b30e6805a",
    ('lopsided', 'disc', 60, 24, 2.0, 2): "c808ab81b76e6ed5",
    ('lopsided', 'disc', 60, 24, 2.0, 4): "2c17cb2370510694",
    ('lopsided', 'disc', 60, 24, 2.43, 2): "a8784f8231a918fe",
    ('lopsided', 'disc', 60, 24, 2.43, 4): "ac1890bbbc3c9d5a",
    ('lopsided', 'disc', 96, 36, 1.5, 2): "c22ec691d2bd7d29",
    ('lopsided', 'disc', 96, 36, 1.5, 4): "5a24463f42423658",
    ('lopsided', 'disc', 96, 36, 2.0, 2): "3cc5fd7a64f022c0",
    ('lopsided', 'disc', 96, 36, 2.0, 4): "e59cb872dc231e6b",
    ('lopsided', 'disc', 96, 36, 2.43, 2): "798fd5547e99f5be",
    ('lopsided', 'disc', 96, 36, 2.43, 4): "56136c32901169f3",
    ('lopsided', 'fill', 60, 24, 1.5, 2): "fdee747fe6895748",
    ('lopsided', 'fill', 60, 24, 1.5, 4): "fec46bd80124b118",
    ('lopsided', 'fill', 60, 24, 2.0, 2): "f909f69ed80981ab",
    ('lopsided', 'fill', 60, 24, 2.0, 4): "7b76884fe582568d",
    ('lopsided', 'fill', 60, 24, 2.43, 2): "0a972857787edd07",
    ('lopsided', 'fill', 60, 24, 2.43, 4): "c5ee5ba602e78714",
    ('lopsided', 'fill', 96, 36, 1.5, 2): "f4959dedc7ba0493",
    ('lopsided', 'fill', 96, 36, 1.5, 4): "3b536792951d327a",
    ('lopsided', 'fill', 96, 36, 2.0, 2): "546e101076208014",
    ('lopsided', 'fill', 96, 36, 2.0, 4): "f471d2571216a3be",
    ('lopsided', 'fill', 96, 36, 2.43, 2): "e2116eefba09d575",
    ('lopsided', 'fill', 96, 36, 2.43, 4): "da8026f90a623976",
    ('lopsided', 'tiles', 60, 24, 1.5, 2): "f71b466cf05dde5c",
    ('lopsided', 'tiles', 60, 24, 1.5, 4): "074de72bd92151fd",
    ('lopsided', 'tiles', 60, 24, 2.0, 2): "f71b466cf05dde5c",
    ('lopsided', 'tiles', 60, 24, 2.0, 4): "074de72bd92151fd",
    ('lopsided', 'tiles', 60, 24, 2.43, 2): "f71b466cf05dde5c",
    ('lopsided', 'tiles', 60, 24, 2.43, 4): "074de72bd92151fd",
    ('lopsided', 'tiles', 96, 36, 1.5, 2): "26a61f885baaaca6",
    ('lopsided', 'tiles', 96, 36, 1.5, 4): "9d0404cbf5c3ecda",
    ('lopsided', 'tiles', 96, 36, 2.0, 2): "26a61f885baaaca6",
    ('lopsided', 'tiles', 96, 36, 2.0, 4): "9d0404cbf5c3ecda",
    ('lopsided', 'tiles', 96, 36, 2.43, 2): "26a61f885baaaca6",
    ('lopsided', 'tiles', 96, 36, 2.43, 4): "0be3813a96b50d39",
    ('deep', 'disc', 60, 24, 1.5, 2): "4fa47877000ade5b",
    ('deep', 'disc', 60, 24, 1.5, 4): "8c1a7275cd345943",
    ('deep', 'disc', 60, 24, 2.0, 2): "0425505349b684b8",
    ('deep', 'disc', 60, 24, 2.0, 4): "bf3c07d6e7e8b21b",
    ('deep', 'disc', 60, 24, 2.43, 2): "367d71a2bc9b0a0d",
    ('deep', 'disc', 60, 24, 2.43, 4): "a07b722038f798a9",
    ('deep', 'disc', 96, 36, 1.5, 2): "c7e4beb8753b1179",
    ('deep', 'disc', 96, 36, 1.5, 4): "c5e17c8a8d29ed7c",
    ('deep', 'disc', 96, 36, 2.0, 2): "f00c5a383008eb4a",
    ('deep', 'disc', 96, 36, 2.0, 4): "1e24183f2fb117a0",
    ('deep', 'disc', 96, 36, 2.43, 2): "1193aac40064e2ca",
    ('deep', 'disc', 96, 36, 2.43, 4): "236285400e37b539",
    ('deep', 'fill', 60, 24, 1.5, 2): "ed325b03f3056c25",
    ('deep', 'fill', 60, 24, 1.5, 4): "cc39b2b0c486ec59",
    ('deep', 'fill', 60, 24, 2.0, 2): "b4e099a9d56bbc70",
    ('deep', 'fill', 60, 24, 2.0, 4): "f12863eca0eb72f4",
    ('deep', 'fill', 60, 24, 2.43, 2): "a7a7e0cd03d7925f",
    ('deep', 'fill', 60, 24, 2.43, 4): "c1de542e92dbeb3b",
    ('deep', 'fill', 96, 36, 1.5, 2): "ff1258a4d3af44d7",
    ('deep', 'fill', 96, 36, 1.5, 4): "852a2f11694a8c68",
    ('deep', 'fill', 96, 36, 2.0, 2): "d5e8d5ad7ea5c982",
    ('deep', 'fill', 96, 36, 2.0, 4): "aa6c2cb32ffafbb7",
    ('deep', 'fill', 96, 36, 2.43, 2): "20628c0d502857ca",
    ('deep', 'fill', 96, 36, 2.43, 4): "586d12f3953ace41",
    ('deep', 'tiles', 60, 24, 1.5, 2): "50d0c200eb826ace",
    ('deep', 'tiles', 60, 24, 1.5, 4): "b3ac740bd9ce0bb3",
    ('deep', 'tiles', 60, 24, 2.0, 2): "50d0c200eb826ace",
    ('deep', 'tiles', 60, 24, 2.0, 4): "b3ac740bd9ce0bb3",
    ('deep', 'tiles', 60, 24, 2.43, 2): "50d0c200eb826ace",
    ('deep', 'tiles', 60, 24, 2.43, 4): "b3ac740bd9ce0bb3",
    ('deep', 'tiles', 96, 36, 1.5, 2): "b2d973e483dd8af7",
    ('deep', 'tiles', 96, 36, 1.5, 4): "0ce5b977f20b2982",
    ('deep', 'tiles', 96, 36, 2.0, 2): "b2d973e483dd8af7",
    ('deep', 'tiles', 96, 36, 2.0, 4): "0ce5b977f20b2982",
    ('deep', 'tiles', 96, 36, 2.43, 2): "b2d973e483dd8af7",
    ('deep', 'tiles', 96, 36, 2.43, 4): "1e4315e23313883d",
    ('single', 'disc', 60, 24, 1.5, 2): "d4a252c01d7e1c9b",
    ('single', 'disc', 60, 24, 1.5, 4): "8ce2b1c3ee0b32f2",
    ('single', 'disc', 60, 24, 2.0, 2): "81493e3306cdfe2c",
    ('single', 'disc', 60, 24, 2.0, 4): "e33972e85d4b935d",
    ('single', 'disc', 60, 24, 2.43, 2): "1f41362f4e3fee55",
    ('single', 'disc', 60, 24, 2.43, 4): "d3e36b13299b7c75",
    ('single', 'disc', 96, 36, 1.5, 2): "d894d2b39c46eaeb",
    ('single', 'disc', 96, 36, 1.5, 4): "198b26a289b67f89",
    ('single', 'disc', 96, 36, 2.0, 2): "2d76f44c0e1d147c",
    ('single', 'disc', 96, 36, 2.0, 4): "4e6cfb06349b34cd",
    ('single', 'disc', 96, 36, 2.43, 2): "0212ad4dd5b9f40a",
    ('single', 'disc', 96, 36, 2.43, 4): "d3f76445e8b00224",
    ('single', 'fill', 60, 24, 1.5, 2): "be1659e494d0ade6",
    ('single', 'fill', 60, 24, 1.5, 4): "a16142463aea4362",
    ('single', 'fill', 60, 24, 2.0, 2): "86d3eb9343c89d39",
    ('single', 'fill', 60, 24, 2.0, 4): "a1d11f0c554c48cf",
    ('single', 'fill', 60, 24, 2.43, 2): "b9f1cf0b271c445d",
    ('single', 'fill', 60, 24, 2.43, 4): "950ae444deff3406",
    ('single', 'fill', 96, 36, 1.5, 2): "81ec2a64cf671d6e",
    ('single', 'fill', 96, 36, 1.5, 4): "046b5bae64e6d63c",
    ('single', 'fill', 96, 36, 2.0, 2): "9f7be23dee8949a7",
    ('single', 'fill', 96, 36, 2.0, 4): "8751260d87e72ef2",
    ('single', 'fill', 96, 36, 2.43, 2): "2939d5cb2a5b9cd0",
    ('single', 'fill', 96, 36, 2.43, 4): "86de5c51011ee717",
    ('single', 'tiles', 60, 24, 1.5, 2): "2c47f168c4085b55",
    ('single', 'tiles', 60, 24, 1.5, 4): "50816f30d6564d31",
    ('single', 'tiles', 60, 24, 2.0, 2): "2c47f168c4085b55",
    ('single', 'tiles', 60, 24, 2.0, 4): "50816f30d6564d31",
    ('single', 'tiles', 60, 24, 2.43, 2): "2c47f168c4085b55",
    ('single', 'tiles', 60, 24, 2.43, 4): "50816f30d6564d31",
    ('single', 'tiles', 96, 36, 1.5, 2): "b29b508e26d74bf6",
    ('single', 'tiles', 96, 36, 1.5, 4): "cad1853faf567c5b",
    ('single', 'tiles', 96, 36, 2.0, 2): "b29b508e26d74bf6",
    ('single', 'tiles', 96, 36, 2.0, 4): "cad1853faf567c5b",
    ('single', 'tiles', 96, 36, 2.43, 2): "b29b508e26d74bf6",
    ('single', 'tiles', 96, 36, 2.43, 4): "7861aedce8780d91",
}


def test_every_ring_shape_is_covered():
    """A shape added without a pin would be tested by nothing."""
    assert set(SHAPES) == set(RING_SHAPES)
    assert len(FRAME_PINS) == len(CASES)


def test_the_cache_draws_the_frame_the_uncached_renderer_drew():
    """Byte for byte, cache on and cache off, against the pinned frames."""
    uncached = {}
    was = set_sample_cache_enabled(False)
    try:
        for case in CASES:
            uncached[case] = digest(case)
    finally:
        set_sample_cache_enabled(was)

    clear_sample_cache()
    cached = {case: digest(case) for case in CASES}

    differ = [c for c in CASES if cached[c] != uncached[c]]
    assert not differ, f"the cache changed {len(differ)} frames, first {differ[0]}"
    moved = [c for c in CASES if cached[c] != FRAME_PINS[c]]
    assert not moved, (
        f"{len(moved)} frames no longer match their pin, first {moved[0]}: "
        f"{FRAME_PINS[moved[0]]} -> {cached[moved[0]]}"
    )


def test_one_geometry_is_planned_once_however_many_frames_it_draws():
    """The point of the whole thing: a live scan repaints the same widget."""
    tree = build_tree("lopsided")
    clear_sample_cache()
    compute_sunburst(
        tree, 96, 36, max_depth=2, metric="logical", cell_aspect=2.0,
        panel_bg=(30, 30, 30), shape="tiles",
    )
    assert len(_PLAN_CACHE) == 1
    plan = next(iter(_PLAN_CACHE.values()))

    for _ in range(5):
        compute_sunburst(
            build_tree("deep"), 96, 36, max_depth=2, metric="logical",
            cell_aspect=2.0, panel_bg=(30, 30, 30), shape="tiles",
        )

    assert len(_PLAN_CACHE) == 1
    assert next(iter(_PLAN_CACHE.values())) is plan


@pytest.mark.parametrize(
    "changed",
    [
        {"char_width": 97},
        {"char_height": 37},
        {"cell_aspect": 2.43},
        {"max_depth": 3},
        {"shape": "disc"},
    ],
)
def test_every_parameter_the_geometry_depends_on_is_in_the_key(changed):
    """A missing key parameter is a stale plan, which is a wrong picture."""
    tree = build_tree("lopsided")
    base = dict(
        char_width=96, char_height=36, cell_aspect=2.0, max_depth=2,
        shape="tiles",
    )
    other = {**base, **changed}

    def draw(args):
        layout = compute_sunburst(
            tree, args["char_width"], args["char_height"],
            max_depth=args["max_depth"], metric="logical",
            cell_aspect=args["cell_aspect"], panel_bg=(30, 30, 30),
            shape=args["shape"],
        )
        return hashlib.sha256(repr(layout.frame).encode()).hexdigest()

    clear_sample_cache()
    first = draw(base)
    draw(other)
    assert draw(base) == first, "the second geometry poisoned the first"
    assert len(_PLAN_CACHE) == 2


def test_the_cache_is_bounded_by_entries():
    """Each plan is a few MB; an unbounded one would follow a resize."""
    tree = build_tree("lopsided")
    clear_sample_cache()
    for width in range(60, 60 + _PLAN_CACHE_LIMIT + 3):
        compute_sunburst(
            tree, width, 30, max_depth=2, metric="logical", cell_aspect=2.0,
            panel_bg=(30, 30, 30), shape="tiles",
        )
    assert len(_PLAN_CACHE) == _PLAN_CACHE_LIMIT


def test_the_cache_is_bounded_by_samples_too():
    """Four entries is not a memory bound when one chart is 30x the other."""
    tree = build_tree("lopsided")
    clear_sample_cache()
    for height in range(200, 204):
        compute_sunburst(
            tree, 400, height, max_depth=2, metric="logical", cell_aspect=2.0,
            panel_bg=(30, 30, 30), shape="tiles",
        )
        held = sum(len(plan.depth) for plan in _PLAN_CACHE.values())
        assert held <= _PLAN_CACHE_MAX_SAMPLES or len(_PLAN_CACHE) == 1
    # One 400x200 chart is 640k subsamples on its own, so it is the only
    # thing the cache can hold -- and it does hold it, rather than falling
    # back to recomputing the geometry every frame.
    assert len(_PLAN_CACHE) == 1


def test_hit_test_reads_the_geometry_rather_than_the_plan():
    """Mouse lookups do their own arithmetic and must not have moved."""
    tree = build_tree("deep")
    for shape in RING_SHAPES:
        clear_sample_cache()
        was = set_sample_cache_enabled(False)
        try:
            layout = compute_sunburst(
                tree, 96, 36, max_depth=3, metric="logical", cell_aspect=2.0,
                panel_bg=(30, 30, 30), shape=shape,
            )
            uncached = [
                (x, y, layout.hit_test(x, y))
                for y in range(0, 36, 3)
                for x in range(0, 96, 5)
            ]
        finally:
            set_sample_cache_enabled(was)
        layout = compute_sunburst(
            tree, 96, 36, max_depth=3, metric="logical", cell_aspect=2.0,
            panel_bg=(30, 30, 30), shape=shape,
        )
        for x, y, arc in uncached:
            hit = layout.hit_test(x, y)
            assert (hit is None) == (arc is None), (shape, x, y)
            if arc is not None:
                assert hit.node.path == arc.node.path, (shape, x, y)
                assert hit.depth == arc.depth
