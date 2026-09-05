"""Regenerate docs/images/sunburst.png and docs/images/monitor.png.

Both hero shots are photographs of the real TUI, not mock-ups: this script
exports a clean copy of the repository, drives DiskTide inside a tmux PTY at
one fixed geometry, captures the pane with its escape sequences intact, and
rasterizes those cells to PNG. Nothing in either image is drawn by hand.

The Monitor shot needs history, so the script replays this repository's own
git log into the snapshot store -- one scan per commit, stamped with that
commit's date. The trend chart is therefore DiskTide's real growth curve,
measured by DiskTide.

The Explorer shot needs the opposite: a checkout as it is worked in, not as
it is tracked. A virtualenv and the byte-caches are put back from a fixed
manifest before the last scan -- see EPHEMERA.

Everything lives in a throwaway XDG root and a staging directory that are
removed on the way out; the developer's own database and config are never
touched.

    uv run --with pillow,rich python tool/gen_readme_shots.py

Needs `tmux` on PATH and a DejaVu Sans Mono installation. The full-width
plus in Monitor Center's delta legend is not in DejaVu, so a CJK fallback
is required for that one glyph -- see FALLBACK_FONTS.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratchguard  # noqa: E402 - needs tool/ on the path first

REPO = Path(__file__).resolve().parent.parent
IMAGES = REPO / "docs" / "images"

# One geometry for both images: they sit side by side in the README, so a
# difference in either axis would be visible as a size jump.
COLS, ROWS = 130, 38
# tmux reports no pixel size, so DiskTide's cell-aspect probe falls back to
# exactly 2.0 and lays the sunburst disc out for square-ish cells. Rasterize
# at any other ratio and the circle comes out an ellipse.
CELL_W, CELL_H = 10, 20
PAD = 16
FONT_SIZE = 16

# Deliberately a plain, public path: it is what the breadcrumb shows. It is
# also a whole checkout plus its ephemera, written once per commit in the
# history, so it goes through the scratch guard before the first export.
STAGE = Path("/tmp/disktide")
#: A checkout is well under two thousand entries; EPHEMERA adds a few dozen.
STAGE_ENTRIES = 4000
SOCKET = "disktide-shots"
MONITOR_LABEL = "disktide dev"
COMMIT_STEP = 1  # every commit; raise to 2 for a faster, sparser trend

MONO_FONTS = [
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]
BOLD_FONTS = [
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
]
FALLBACK_FONTS = [
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

DEFAULT_FG = (224, 224, 224)
DEFAULT_BG = (30, 30, 30)

# Block Elements are drawn geometrically rather than from the font, which is
# what xterm.js, kitty and wezterm do. DiskTide anti-aliases the sunburst rim
# with half blocks, so font-drawn approximations would visibly notch the disc.
BLOCKS = {
    "▀": (0, 0, 1, 1 / 2), "▄": (0, 1 / 2, 1, 1),
    "█": (0, 0, 1, 1), "▌": (0, 0, 1 / 2, 1),
    "▐": (1 / 2, 0, 1, 1),
    "▁": (0, 7 / 8, 1, 1), "▂": (0, 3 / 4, 1, 1),
    "▃": (0, 5 / 8, 1, 1), "▅": (0, 3 / 8, 1, 1),
    "▆": (0, 1 / 4, 1, 1), "▇": (0, 1 / 8, 1, 1),
    "▉": (0, 0, 7 / 8, 1), "▊": (0, 0, 3 / 4, 1),
    "▋": (0, 0, 5 / 8, 1), "▍": (0, 0, 3 / 8, 1),
    "▎": (0, 0, 1 / 4, 1), "▏": (0, 0, 1 / 8, 1),
    "▔": (0, 0, 1, 1 / 8), "▕": (7 / 8, 0, 1, 1),
}
_UL, _UR, _LL, _LR = (0, 0), (1, 0), (0, 1), (1, 1)
QUADRANTS = {
    "▖": (_LL,), "▗": (_LR,), "▘": (_UL,), "▝": (_UR,),
    "▙": (_UL, _LL, _LR), "▚": (_UL, _LR),
    "▛": (_UL, _UR, _LL), "▜": (_UL, _UR, _LR),
    "▞": (_UR, _LL), "▟": (_UR, _LL, _LR),
}
SHADES = {"░": 0.25, "▒": 0.5, "▓": 0.75}
# U+2B58 HEAVY CIRCLE is DiskTide's mark and is absent from every font a
# stock Linux ships, so it is stroked as the ring it is rather than left to
# render as a tofu box. Radius and stroke are fractions of the smaller cell
# axis, which is what keeps the mark inside its advance width.
RINGS = {"⭘": (0.38, 0.14)}


# --------------------------------------------------------------------------
# staging and history
# --------------------------------------------------------------------------

def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def export_tree(sha: str) -> None:
    """Replace STAGE with the worktree at `sha` -- tracked files only.

    Every snapshot must share a root path for the trend to line up, so the
    directory is emptied in place rather than recreated per commit.
    """
    STAGE.mkdir(parents=True, exist_ok=True)
    for child in STAGE.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    archive = subprocess.Popen(
        ["git", "-C", str(REPO), "archive", sha], stdout=subprocess.PIPE
    )
    subprocess.run(["tar", "-x", "-C", str(STAGE)], stdin=archive.stdout, check=True)
    archive.wait()


# A `git archive` export is tracked files only, and a tree of nothing but
# tracked files is not what anyone's disk looks like: the disc came out all
# code, which is the one picture that misrepresents both the checkout and
# the tool. On a working checkout the ephemeral mass -- a virtualenv, the
# byte-caches, the test cache -- outweighs everything a human wrote, and
# that is the story a disk-usage tool exists to tell. So it is put back.
#
# From a table, though, and never by copying the developer's own .venv: the
# README's alt text quotes the legend percentages, so a manifest that moved
# with whatever happens to be installed would turn that alt text into a lie
# on the next reshoot. Only names and sizes matter here -- the bytes are
# filler -- and the sizes are picked to land the disc on ephemeral 55% /
# code 28% / media 10% / docs 7%, which is close to what this repository's
# real working checkout reports, with `.venv/` at about half the tree so
# the tree panel leads with it the way a real one does.
#
# Seeded onto the final HEAD export only. `build_history` has to keep
# scanning tracked files alone, or every point on the trend would carry the
# same constant 4 MiB and the curve would stop being the repository's own.
_SITE = ".venv/lib/python3.12/site-packages/"
EPHEMERA: tuple[tuple[str, int], ...] = (
    (".venv/pyvenv.cfg", 189),
    (".venv/bin/activate", 2_063),
    (".venv/bin/disktide", 251),
    (".venv/bin/pytest", 268),

    (_SITE + "textual/__init__.py", 6_142),
    (_SITE + "textual/app.py", 148_820),
    (_SITE + "textual/widget.py", 96_437),
    (_SITE + "textual/dom.py", 41_286),
    (_SITE + "textual/screen.py", 63_912),
    (_SITE + "textual/message_pump.py", 24_508),
    (_SITE + "textual/geometry.py", 32_664),
    (_SITE + "textual/events.py", 21_037),
    (_SITE + "textual/reactive.py", 23_915),
    (_SITE + "textual/_compositor.py", 39_180),
    (_SITE + "textual/containers.py", 6_704),
    (_SITE + "textual/__pycache__/__init__.cpython-312.pyc", 5_318),
    (_SITE + "textual/__pycache__/app.cpython-312.pyc", 121_446),
    (_SITE + "textual/__pycache__/widget.cpython-312.pyc", 78_902),
    (_SITE + "textual/__pycache__/dom.cpython-312.pyc", 33_155),
    (_SITE + "textual/__pycache__/screen.cpython-312.pyc", 52_017),
    (_SITE + "textual/__pycache__/message_pump.cpython-312.pyc", 19_884),
    (_SITE + "textual/__pycache__/geometry.cpython-312.pyc", 26_573),
    (_SITE + "textual/__pycache__/events.cpython-312.pyc", 17_260),
    (_SITE + "textual/__pycache__/reactive.cpython-312.pyc", 19_006),
    (_SITE + "textual/__pycache__/_compositor.cpython-312.pyc", 31_902),
    (_SITE + "textual/__pycache__/containers.cpython-312.pyc", 5_744),
    (_SITE + "textual/css/_style_properties.py", 44_562),
    (_SITE + "textual/css/stylesheet.py", 34_129),
    (_SITE + "textual/css/parse.py", 25_842),
    (_SITE + "textual/css/tokenize.py", 18_760),
    (_SITE + "textual/css/__pycache__/_style_properties.cpython-312.pyc", 36_118),
    (_SITE + "textual/css/__pycache__/stylesheet.cpython-312.pyc", 27_744),
    (_SITE + "textual/css/__pycache__/parse.cpython-312.pyc", 20_991),
    (_SITE + "textual/css/__pycache__/tokenize.cpython-312.pyc", 15_302),
    (_SITE + "textual/widgets/_data_table.py", 92_318),
    (_SITE + "textual/widgets/_tree.py", 51_074),
    (_SITE + "textual/widgets/_input.py", 46_211),
    (_SITE + "textual/widgets/_button.py", 12_506),
    (_SITE + "textual/widgets/_static.py", 3_072),
    (_SITE + "textual/widgets/__pycache__/_data_table.cpython-312.pyc", 74_650),
    (_SITE + "textual/widgets/__pycache__/_tree.cpython-312.pyc", 41_338),
    (_SITE + "textual/widgets/__pycache__/_input.cpython-312.pyc", 37_486),
    (_SITE + "textual/widgets/__pycache__/_button.cpython-312.pyc", 10_204),
    (_SITE + "textual/widgets/__pycache__/_static.cpython-312.pyc", 2_601),

    (_SITE + "rich/__init__.py", 6_269),
    (_SITE + "rich/console.py", 99_884),
    (_SITE + "rich/text.py", 47_312),
    (_SITE + "rich/table.py", 39_506),
    (_SITE + "rich/segment.py", 24_781),
    (_SITE + "rich/style.py", 27_310),
    (_SITE + "rich/markup.py", 8_452),
    (_SITE + "rich/syntax.py", 35_744),
    (_SITE + "rich/progress.py", 59_138),
    (_SITE + "rich/traceback.py", 32_006),
    (_SITE + "rich/__pycache__/__init__.cpython-312.pyc", 5_402),
    (_SITE + "rich/__pycache__/console.cpython-312.pyc", 81_337),
    (_SITE + "rich/__pycache__/text.cpython-312.pyc", 39_640),
    (_SITE + "rich/__pycache__/table.cpython-312.pyc", 32_118),
    (_SITE + "rich/__pycache__/segment.cpython-312.pyc", 20_669),
    (_SITE + "rich/__pycache__/style.cpython-312.pyc", 22_884),
    (_SITE + "rich/__pycache__/markup.cpython-312.pyc", 7_120),
    (_SITE + "rich/__pycache__/syntax.cpython-312.pyc", 29_450),
    (_SITE + "rich/__pycache__/progress.cpython-312.pyc", 48_602),
    (_SITE + "rich/__pycache__/traceback.cpython-312.pyc", 26_155),

    (_SITE + "aiohttp/__init__.py", 8_034),
    (_SITE + "aiohttp/client.py", 62_190),
    (_SITE + "aiohttp/web.py", 22_468),
    (_SITE + "aiohttp/connector.py", 58_907),
    (_SITE + "aiohttp/helpers.py", 34_112),
    (_SITE + "aiohttp/_websocket.cpython-312-x86_64-linux-gnu.so", 214_392),
    (_SITE + "aiohttp/_http_parser.cpython-312-x86_64-linux-gnu.so", 476_856),
    (_SITE + "aiohttp/__pycache__/__init__.cpython-312.pyc", 6_915),
    (_SITE + "aiohttp/__pycache__/client.cpython-312.pyc", 50_663),
    (_SITE + "aiohttp/__pycache__/web.cpython-312.pyc", 18_442),
    (_SITE + "aiohttp/__pycache__/connector.cpython-312.pyc", 47_708),
    (_SITE + "aiohttp/__pycache__/helpers.cpython-312.pyc", 28_330),

    (_SITE + "pytest/__init__.py", 5_112),
    (_SITE + "pytest/__pycache__/__init__.cpython-312.pyc", 4_602),
    (_SITE + "_pytest/fixtures.py", 68_244),
    (_SITE + "_pytest/python.py", 61_130),
    (_SITE + "_pytest/__pycache__/fixtures.cpython-312.pyc", 56_211),
    (_SITE + "_pytest/__pycache__/python.cpython-312.pyc", 50_338),
    (_SITE + "_pytest/config/__init__.py", 74_918),
    (_SITE + "_pytest/config/__pycache__/__init__.cpython-312.pyc", 60_744),
    (_SITE + "_pytest/assertion/rewrite.py", 52_366),
    (_SITE + "_pytest/assertion/__pycache__/rewrite.cpython-312.pyc", 43_190),
    (_SITE + "_pytest/mark/structures.py", 21_405),
    (_SITE + "_pytest/mark/__pycache__/structures.cpython-312.pyc", 17_662),

    (_SITE + "pygments/__init__.py", 2_959),
    (_SITE + "pygments/lexer.py", 34_128),
    (_SITE + "pygments/token.py", 6_226),
    (_SITE + "pygments/__pycache__/__init__.cpython-312.pyc", 2_486),
    (_SITE + "pygments/__pycache__/lexer.cpython-312.pyc", 27_690),
    (_SITE + "pygments/__pycache__/token.cpython-312.pyc", 4_902),
    (_SITE + "pygments/lexers/python.py", 54_236),
    (_SITE + "pygments/lexers/data.py", 26_814),
    (_SITE + "pygments/lexers/__pycache__/python.cpython-312.pyc", 44_881),
    (_SITE + "pygments/lexers/__pycache__/data.cpython-312.pyc", 21_450),
    (_SITE + "pygments/formatters/terminal256.py", 11_907),
    (_SITE + "pygments/formatters/__pycache__/terminal256.cpython-312.pyc", 9_804),
    (_SITE + "pygments/styles/__init__.py", 3_814),
    (_SITE + "pygments/styles/monokai.py", 5_302),

    (_SITE + "textual-1.0.0.dist-info/METADATA", 8_214),
    (_SITE + "textual-1.0.0.dist-info/RECORD", 21_060),

    # The package's own byte-cache, under two interpreters, the way an
    # editable install tested across a version matrix leaves it.
    ("src/disktide/__pycache__/__init__.cpython-312.pyc", 594),
    ("src/disktide/__pycache__/__main__.cpython-312.pyc", 84_326),
    ("src/disktide/__pycache__/app.cpython-312.pyc", 18_604),
    ("src/disktide/__pycache__/config.cpython-312.pyc", 18_815),
    ("src/disktide/__pycache__/glyphs.cpython-312.pyc", 1_110),
    ("src/disktide/__pycache__/metrics.cpython-312.pyc", 2_488),
    ("src/disktide/__pycache__/paths.cpython-312.pyc", 2_961),
    ("src/disktide/__pycache__/rendering.cpython-312.pyc", 3_204),
    ("src/disktide/__pycache__/visualization_formatting.cpython-312.pyc", 4_137),
    ("src/disktide/__pycache__/__init__.cpython-313.pyc", 601),
    ("src/disktide/__pycache__/__main__.cpython-313.pyc", 85_102),
    ("src/disktide/__pycache__/app.cpython-313.pyc", 18_825),
    ("src/disktide/__pycache__/config.cpython-313.pyc", 19_333),
    ("src/disktide/__pycache__/glyphs.cpython-313.pyc", 1_112),
    ("src/disktide/__pycache__/metrics.cpython-313.pyc", 2_510),
    ("src/disktide/__pycache__/paths.cpython-313.pyc", 2_988),
    ("src/disktide/__pycache__/rendering.cpython-313.pyc", 3_236),
    ("src/disktide/__pycache__/visualization_formatting.cpython-313.pyc", 4_174),

    (".pytest_cache/CACHEDIR.TAG", 191),
    (".pytest_cache/.gitignore", 37),
    (".pytest_cache/README.md", 302),
    (".pytest_cache/v/cache/nodeids", 104_918),
    (".pytest_cache/v/cache/lastfailed", 592),
    (".pytest_cache/v/cache/stepwise", 3),
)

# Repeated to length: the scan reads sizes, never contents.
FILLER = b"# regenerable payload -- only this file's name and size matter\n"


def seed_ephemera(root: Path) -> int:
    """Write EPHEMERA under `root` and report the bytes added."""
    total = 0
    for relative, size in EPHEMERA:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = FILLER * (size // len(FILLER) + 1)
        path.write_bytes(blob[:size])
        total += size
    return total


def commit_log() -> list[tuple[str, datetime]]:
    rows = []
    for line in git("log", "--reverse", "--format=%H %cI").splitlines():
        sha, iso = line.split(" ", 1)
        rows.append((sha, datetime.fromisoformat(iso).astimezone(timezone.utc)))
    picked = rows[::COMMIT_STEP]
    if picked[-1][0] != rows[-1][0]:
        picked.append(rows[-1])
    return picked


def create_monitor(env: dict[str, str]) -> None:
    subprocess.run(
        [sys.executable, "-m", "disktide", "monitor", "add", str(STAGE),
         "--label", MONITOR_LABEL, "--interval", "6h"],
        env=env, check=True, capture_output=True, text=True,
    )


def build_history() -> int:
    """Scan every exported commit and file it under the monitor's history.

    `Snapshot.from_scan_run` stamps the run's own clock, which would pile
    158 points onto one afternoon. Overwriting the timestamps before the
    save is the whole trick: the repository writes what the dataclass says.
    """
    from disktide.domain.metrics import MetricId
    from disktide.domain.policy import ScanPolicy
    from disktide.domain.scan import ScanRequest, ScanStatus
    from disktide.domain.snapshot import Snapshot
    from disktide.repositories import default_snapshot_repository
    from disktide.services.scan import ScanService

    repository = default_snapshot_repository()
    repository.connect()
    definition = repository.list_monitors()[0]
    # One policy for every scan: snapshots taken under different policies are
    # not comparable, and an incompatible segment breaks the trend line.
    policy = ScanPolicy()
    service = ScanService()
    saved = 0
    try:
        commits = commit_log()
        for index, (sha, when) in enumerate(commits, 1):
            export_tree(sha)
            run = service.execute(service.create_run(ScanRequest(
                path=str(STAGE), metric=MetricId.LOGICAL,
                policy=policy, source="monitor",
            )))
            if run.status is not ScanStatus.COMPLETED or run.root is None:
                print(f"  skipped {sha[:8]}: {run.status.value}")
                continue
            snapshot = Snapshot.from_scan_run(
                run,
                monitor_id=definition.id,
                monitor_revision=definition.revision,
            )
            snapshot.timestamp = when
            snapshot.created_at = when
            snapshot.started_at = when
            snapshot.finished_at = when
            repository.save_snapshot(snapshot, run.root)
            saved += 1
            if index % 25 == 0 or index == len(commits):
                print(f"  {index}/{len(commits)} commits "
                      f"({when:%Y-%m-%d}, {snapshot.total_size:,} bytes)")
    finally:
        repository.close()
    return saved


# --------------------------------------------------------------------------
# tmux capture
# --------------------------------------------------------------------------

def tmux(*args: str) -> str:
    """Run one tmux command on a private server and hand back its stdout.

    Failures are not raised: `kill-server` on a server that was never
    started is a normal part of the flow.
    """
    return subprocess.run(
        ["tmux", "-L", SOCKET, *args],
        capture_output=True, text=True,
    ).stdout


def start_pane(env: dict[str, str]) -> None:
    """Open a pane that is already the final size when the app starts.

    A detached session ignores `new-session -x/-y` once anything redraws and
    snaps back to 80x24; `window-size manual` plus an explicit resize is what
    makes the geometry stick. The app is launched only afterwards, so it
    never lays out for the wrong width.
    """
    tmux("kill-server")
    time.sleep(0.5)
    tmux("new-session", "-d", "-s", "cap", "-x", str(COLS), "-y", str(ROWS), "bash")
    tmux("set", "-g", "window-size", "manual")
    tmux("resize-window", "-t", "cap", "-x", str(COLS), "-y", str(ROWS))
    size = tmux("display", "-pt", "cap", "#{pane_width}x#{pane_height}").strip()
    if size != f"{COLS}x{ROWS}":
        raise RuntimeError(f"tmux pane is {size}, expected {COLS}x{ROWS}")
    exports = " ".join(
        f"{name}={env[name]}"
        for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME",
                     "XDG_CACHE_HOME", "XDG_STATE_HOME")
    )
    tmux("send-keys", "-t", "cap",
         f"clear; env {exports} COLORTERM=truecolor TERM=xterm-256color "
         f"{sys.executable} -m disktide", "Enter")


def screen_text() -> str:
    return tmux("capture-pane", "-pt", "cap")


def wait_for(*needles: str, timeout: float = 120.0) -> None:
    """Block until every marker is on screen, then until the screen stops moving.

    Sleeping a fixed number of seconds after each keystroke either wastes
    time or photographs a half-drawn frame; the markers say the right screen
    arrived and the stability check says it finished drawing.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(needle in screen_text() for needle in needles):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError(f"timed out waiting for {needles!r}")
    previous = None
    while time.monotonic() < deadline:
        current = screen_text()
        if current == previous:
            return
        previous = current
        time.sleep(1.0)


def capture(path: Path) -> None:
    """Dump the pane with colour (-e) and trailing cells (-N) preserved.

    Without -N tmux drops each line's trailing blanks, and the panel
    backgrounds they carried turn into a patchwork of bare terminal.
    """
    path.write_text(tmux("capture-pane", "-epNt", "cap"), encoding="utf-8")


# --------------------------------------------------------------------------
# rasterizer
# --------------------------------------------------------------------------

_RING_MASKS: dict[tuple[str, int], object] = {}


def ring_mask(char: str, span: int):
    """An anti-aliased stroked circle, drawn big and shrunk to one cell.

    Pillow does not anti-alias `ellipse`, and a seven-pixel radius drawn
    straight into the grid comes out as a lopsided C. Supersampling gives
    the mark the smooth stroke a real glyph would have.
    """
    from PIL import Image, ImageDraw

    key = (char, span)
    if key not in _RING_MASKS:
        scale = 8
        radius, stroke = RINGS[char]
        width, height = span * CELL_W * scale, CELL_H * scale
        unit = min(width, height)
        tile = Image.new("L", (width, height), 0)
        r = radius * unit
        cx, cy = width / 2, height / 2
        ImageDraw.Draw(tile).ellipse(
            (cx - r, cy - r, cx + r, cy + r),
            outline=255, width=max(1, round(stroke * unit)),
        )
        _RING_MASKS[key] = tile.resize(
            (span * CELL_W, CELL_H), Image.LANCZOS
        )
    return _RING_MASKS[key]


def pick_font(candidates: list[str]) -> str:
    for path in candidates:
        if Path(path).exists():
            return path
    raise RuntimeError(f"no font found among {candidates}")


def cell_width(char: str) -> int:
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


class Glyphs:
    """Per-character font choice that never falls through to .notdef.

    Pillow happily renders a missing codepoint as the font's tofu box, and
    `getmask(...).getbbox()` cannot tell that box from a real glyph. Drawing
    the character and comparing it against a known-absent one can.
    """

    def __init__(self, size: int) -> None:
        from PIL import Image, ImageDraw, ImageFont

        self._image, self._draw = Image, ImageDraw
        self._size = size
        self.regular = ImageFont.truetype(pick_font(MONO_FONTS), size)
        self.bold = ImageFont.truetype(pick_font(BOLD_FONTS), size)
        self.fallbacks = [
            ImageFont.truetype(path, size)
            for path in FALLBACK_FONTS if Path(path).exists()
        ]
        self._notdef = {
            id(font): self._bitmap(font, "\U000f0000")
            for font in (self.regular, self.bold, *self.fallbacks)
        }
        self._cache: dict[tuple[str, bool], object] = {}

    def _bitmap(self, font, char: str) -> bytes:
        box = self._size * 3
        tile = self._image.new("L", (box, box), 0)
        self._draw.Draw(tile).text((box // 3, box // 3), char, font=font, fill=255)
        return tile.tobytes()

    def font_for(self, char: str, bold: bool):
        key = (char, bold)
        if key not in self._cache:
            chosen = None
            for font in ((self.bold if bold else self.regular), *self.fallbacks):
                drawn = self._bitmap(font, char)
                if any(drawn) and drawn != self._notdef[id(font)]:
                    chosen = font
                    break
            self._cache[key] = chosen
        return self._cache[key]


def to_rgb(color):
    """Resolve one SGR colour, or None where the terminal decides.

    `ESC[39m` / `ESC[49m` reach rich as ColorType.DEFAULT, and rich resolves
    those against its light SVG theme -- black ink on white paper. Returning
    None instead lets the caller substitute the surface the app draws on.
    """
    from rich.color import ColorType

    if color is None or color.type is ColorType.DEFAULT:
        return None
    if color.type is ColorType.TRUECOLOR:
        triplet = color.triplet
        return (triplet.red, triplet.green, triplet.blue)
    try:
        triplet = color.get_truecolor()
        return (triplet.red, triplet.green, triplet.blue)
    except Exception:
        return None


def rasterize(src: Path, dst: Path) -> tuple[int, int]:
    from PIL import Image, ImageDraw
    from rich.ansi import AnsiDecoder
    from rich.console import Console

    lines = list(AnsiDecoder().decode(src.read_text(encoding="utf-8")))
    while lines and not lines[-1].plain.strip():
        lines.pop()
    rows = len(lines)
    cols = max(
        (sum(cell_width(c) for c in line.plain) for line in lines), default=COLS
    )
    console = Console(width=cols + 2, color_system="truecolor")

    # Whatever the app paints in its top-left cell is header chrome on every
    # screen, so it is the honest stand-in for "terminal default background"
    # and for the border padding.
    canvas_bg = DEFAULT_BG
    head = next(iter(lines[0].render(console)), None) if lines else None
    if head is not None and head[1] is not None:
        canvas_bg = to_rgb(head[1].bgcolor) or canvas_bg

    glyphs = Glyphs(FONT_SIZE)
    ascent, descent = glyphs.regular.getmetrics()
    baseline = (CELL_H - (ascent + descent)) // 2 + ascent

    image = Image.new(
        "RGB", (cols * CELL_W + 2 * PAD, rows * CELL_H + 2 * PAD), canvas_bg
    )
    draw = ImageDraw.Draw(image)
    missing: set[str] = set()

    for row, line in enumerate(lines):
        column = 0
        for text, style, _ in line.render(console):
            fg = to_rgb(style.color if style else None) or DEFAULT_FG
            bg = to_rgb(style.bgcolor if style else None)
            bold = bool(style and style.bold)
            if style and style.reverse:
                fg, bg = (bg or canvas_bg), fg
            for char in text:
                span = cell_width(char)
                x0, y0 = PAD + column * CELL_W, PAD + row * CELL_H
                x1, y1 = x0 + span * CELL_W, y0 + CELL_H
                base = bg if bg is not None else canvas_bg
                if bg is not None:
                    draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=bg)
                if char in BLOCKS:
                    bx0, by0, bx1, by1 = BLOCKS[char]
                    draw.rectangle(
                        (round(x0 + bx0 * CELL_W), round(y0 + by0 * CELL_H),
                         round(x0 + bx1 * CELL_W) - 1,
                         round(y0 + by1 * CELL_H) - 1),
                        fill=fg,
                    )
                elif char in QUADRANTS:
                    half_w, half_h = CELL_W // 2, CELL_H // 2
                    for qx, qy in QUADRANTS[char]:
                        qx0, qy0 = x0 + qx * half_w, y0 + qy * half_h
                        draw.rectangle(
                            (qx0, qy0, qx0 + half_w - 1, qy0 + half_h - 1),
                            fill=fg,
                        )
                elif char in SHADES:
                    alpha = SHADES[char]
                    draw.rectangle(
                        (x0, y0, x1 - 1, y1 - 1),
                        fill=tuple(
                            int(b + (f - b) * alpha) for f, b in zip(fg, base)
                        ),
                    )
                elif char in RINGS:
                    image.paste(fg, (x0, y0), ring_mask(char, span))
                elif char != " ":
                    font = glyphs.font_for(char, bold)
                    if font is None:
                        missing.add(char)
                    else:
                        draw.text(
                            (x0 + span * CELL_W / 2, y0 + baseline),
                            char, font=font, fill=fg, anchor="ms",
                        )
                column += span

    if missing:
        codes = " ".join(f"U+{ord(c):04X}" for c in sorted(missing))
        raise RuntimeError(f"no font covers {codes}; the PNG would show tofu")
    image.save(dst)
    return image.size


# --------------------------------------------------------------------------

def main() -> None:
    if shutil.which("tmux") is None:
        raise SystemExit("tmux is required")
    scratchguard.claim(STAGE, entries=STAGE_ENTRIES, label="readme-stage")
    scratch = Path(tempfile.mkdtemp(
        dir=scratchguard.scratch_dir("readme-shots", entries=64)
    ))
    captures = scratch / "captures"
    captures.mkdir()
    for name in ("config", "data", "cache", "state"):
        (scratch / name).mkdir()
    # An isolated XDG root: the shot must not read, or write, the developer's
    # own monitors, snapshots or preferences. This process replays the git
    # history in-process, so it needs the same redirection the TUI gets.
    xdg = {
        "XDG_CONFIG_HOME": str(scratch / "config"),
        "XDG_DATA_HOME": str(scratch / "data"),
        "XDG_CACHE_HOME": str(scratch / "cache"),
        "XDG_STATE_HOME": str(scratch / "state"),
    }
    os.environ.update(xdg)
    env = dict(os.environ)

    try:
        print(f"staging {STAGE} and replaying history into {scratch}")
        export_tree("HEAD")
        create_monitor(env)
        count = build_history()
        export_tree("HEAD")
        seeded = seed_ephemera(STAGE)
        print(f"  {count} snapshots saved, {seeded:,} bytes of ephemera staged")

        print(f"driving DiskTide at {COLS}x{ROWS}")
        start_pane(env)
        wait_for("Enter explore")
        tmux("send-keys", "-t", "cap", "C-u")
        time.sleep(0.5)
        tmux("send-keys", "-t", "cap", "-l", str(STAGE))
        time.sleep(0.5)
        tmux("send-keys", "-t", "cap", "Enter")
        # The file-type legend only lands once the scan has finished and the
        # sunburst has drawn its last ring; "ephemeral" additionally proves
        # the seeded tree, not a bare export, is what was measured. The
        # footer marker is `ui Nav`, the grouped form the key map redesign
        # left behind -- it was `Nav [U]p` when this script was written and
        # nothing noticed, because a stale needle here reads as a timeout.
        wait_for("Sunburst [F1]", "ui Nav", ".gitignore",
                 "ephemeral ", "code ")
        capture(captures / "sunburst.ans")

        tmux("send-keys", "-t", "cap", "2")
        wait_for("Space-Time Trend", "canonical point(s)", MONITOR_LABEL)
        capture(captures / "monitor.ans")

        for name in ("sunburst", "monitor"):
            size = rasterize(captures / f"{name}.ans", IMAGES / f"{name}.png")
            print(f"  docs/images/{name}.png {size[0]}x{size[1]}")
    finally:
        tmux("kill-server")
        shutil.rmtree(STAGE, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
