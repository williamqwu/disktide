"""Regenerate the figures of the TUI field guide in docs/blogs.

Every figure is a photograph of the real TUI, taken the way the README hero
shots are (`gen_readme_shots.py`, whose tmux helpers and rasterizer this
reuses): DiskTide runs in a tmux PTY at one fixed geometry, the pane is
captured with its escape sequences, and the cells are rasterized to PNG. The
numbered pins on the annotated figures are the only thing drawn on top, and
they are placed by finding text in the same capture, so a layout change moves
them with it -- or fails loudly when the text they point at is gone.

The Explorer and Monitor figures need a home directory with a story in it,
so one is staged at /tmp/home: a sparse disk image, a hardlink mirror, two
unreadable directories, a symlink, caches, and 31 days of monitor history
replayed with back-dated timestamps (a log that grows daily, checkpoints
every third day, a backup that comes and goes, a cache emptied and rebuilt,
and one last day with every kind of change in it). The FS Overview figures
show a made-up server instead of this machine; see FAKE_MOUNTS.

    uv run --with pillow,rich python tool/gen_blog_shots.py [--out DIR]

Like the README shots, the images are not versioned here: they live in the
assets repository (github.com/williamqwu/assets, `disktide/blog/`), which the
post links by absolute URL. `--out` defaults to that checkout cloned next to
this one. The stage, the XDG root and the tmux server are removed on exit.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_readme_shots as shots  # noqa: E402 - needs tool/ on the path first
import scratchguard  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO.parent / "tool-assets" / "disktide" / "blog"

# Wide enough that the tree panel (40%) holds the Diff indicator in full.
COLS, ROWS = 150, 46
SOCKET = "disktide-blog"
STAGE = Path("/tmp/home")
STAGE_ENTRIES = 6000
DAYS = 31
LOCKED = ("private", "projects/legacy/secrets")
KiB, MiB = 1024, 1024 * 1024
TREE_WIDTH = COLS * 2 // 5

PIN_FILL = (255, 176, 32)
PIN_INK = (26, 18, 0)
PIN_RING = (20, 20, 20)
PIN_RADIUS = 13

_BLOCK = random.Random(1).randbytes(MiB)


# --------------------------------------------------------------------------
# the staged home
# --------------------------------------------------------------------------

def put(path: Path, size: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        left = int(size)
        while left:
            chunk = min(left, MiB)
            handle.write(_BLOCK[:chunk])
            left -= chunk


def append(path: Path, size: float) -> None:
    with open(path, "ab") as handle:
        handle.write(_BLOCK[: int(size)])


def settle(path: Path) -> None:
    """Rewrite a file onto a fresh inode.

    xfs keeps speculative preallocation past EOF on a file that is appended
    to over and over, so a daily log reads larger under Allocated than it is
    -- by an amount that drifts as the background trim catches up. A new
    inode written once and closed carries none.
    """
    fresh = path.with_name(path.name + ".settle")
    fresh.write_bytes(path.read_bytes())
    os.replace(fresh, path)


def jitter(rng: random.Random, base: float, spread: float = 0.35) -> int:
    return int(base * (1 + rng.uniform(-spread, spread)))


def build_venv(venv: Path, rng: random.Random) -> None:
    site = venv / "lib/python3.12/site-packages"
    put(venv / "pyvenv.cfg", 200)
    for tool in ("python", "pip", "jupyter", "ipython"):
        put(venv / "bin" / tool, 300)
    for package, count in (("numpy", 60), ("pandas", 80), ("matplotlib", 60),
                           ("scipy", 70), ("jupyterlab", 40)):
        for index in range(count):
            put(site / f"{package}/mod{index:02d}.py", jitter(rng, 24 * KiB))
        for index in range(count // 4):
            put(site / f"{package}/__pycache__/mod{index:02d}.cpython-312.pyc",
                jitter(rng, 20 * KiB))
        put(site / f"{package}/_core.cpython-312-x86_64-linux-gnu.so",
            jitter(rng, 3 * MiB, 0.4))


def build_node_modules(modules: Path, rng: random.Random) -> None:
    """1,080 small files: why `projects/` leads under the Files metric."""
    for package in ("react", "react-dom", "typescript", "webpack", "@babel/core",
                    "lodash", "eslint", "vite", "esbuild", "rollup", "postcss",
                    "jest"):
        put(modules / package / "package.json", 2 * KiB)
        for index in range(60):
            put(modules / package / "lib" / f"f{index:02d}.js", jitter(rng, 18 * KiB))
        for index in range(29):
            put(modules / package / "dist" / f"b{index:02d}.js", jitter(rng, 30 * KiB))


PIP_WHEELS = (
    "numpy", "pandas", "scipy", "matplotlib", "torch_stub", "tqdm", "rich",
    "textual", "pyarrow", "polars", "xarray", "netCDF4", "h5py", "requests",
    "urllib3", "certifi", "idna", "pillow", "sympy", "networkx", "jinja2",
    "markupsafe", "pyyaml", "click", "typer", "pydantic", "attrs", "numba",
    "llvmlite", "cython",
)


def build_pip_cache(pip: Path, rng: random.Random) -> None:
    for index, name in enumerate(PIP_WHEELS):
        put(pip / "wheels" / f"{index:02x}" / f"{name}-1.0-py3-none-any.whl",
            jitter(rng, 1.2 * MiB, 0.5))
    for index in range(40):
        put(pip / "http-v2" / f"{index:02x}" / f"entry{index:02d}.body",
            jitter(rng, 60 * KiB))


def build_day_one(root: Path) -> None:
    rng = random.Random(20260912)

    # A sparse disk image: 512 MiB logical, 64 MiB of it allocated.
    image = root / "vm" / "dev-box.qcow2"
    image.parent.mkdir(parents=True)
    with open(image, "wb") as handle:
        handle.truncate(512 * MiB)
        for offset in range(0, 512, 8):
            handle.seek(offset * MiB)
            handle.write(_BLOCK)
    put(root / "vm" / "README.md", 2 * KiB)

    for index in range(21):
        put(root / f"datasets/climate/raw/station-{index:03d}.csv",
            jitter(rng, 2.7 * MiB, 0.1))
    for name in ("2026-06", "2026-07", "2026-08", "stations", "grid-hourly",
                 "grid-daily", "anomalies", "baseline"):
        put(root / f"datasets/climate/processed/{name}.parquet", jitter(rng, 6 * MiB, 0.3))
    put(root / "datasets/climate/README.md", 4 * KiB)
    for index in range(4):
        put(root / f"datasets/imagery/tiles-{index:02d}.h5", jitter(rng, 12 * MiB, 0.2))
    put(root / "datasets/imagery/index.json", 60 * KiB)
    (root / "latest-dataset").symlink_to("datasets/climate")

    # photos/, mirrored into backup/ by hardlink: Allocated counts both
    # paths, Unique charges the bytes to the one that sorts first.
    for index in range(160):
        put(root / f"photos/2024/IMG_{index:04d}.jpg", jitter(rng, 300 * KiB))
    for index in range(80):
        put(root / f"photos/2025/IMG_{index:04d}.jpg", jitter(rng, 400 * KiB))
    for name in ("hike", "birthday", "timelapse"):
        put(root / f"photos/videos/{name}.mp4", jitter(rng, 20 * MiB, 0.2))
    mirror = root / "backup" / "photos-2024"
    mirror.mkdir(parents=True)
    for source in sorted((root / "photos" / "2024").iterdir()):
        os.link(source, mirror / source.name)
    put(root / "backup/dotfiles.tar.gz", 180 * KiB)

    build_pip_cache(root / ".cache" / "pip", rng)
    hub = root / ".cache/huggingface/hub"
    put(hub / "models--bert-base-uncased/blobs/model.safetensors", 44 * MiB)
    put(hub / "models--bert-base-uncased/blobs/tokenizer.json", 700 * KiB)
    put(hub / "models--t5-small/blobs/model.safetensors", 20 * MiB)

    put(root / "models/llama-mini/model.safetensors", 60 * MiB)
    put(root / "models/llama-mini/config.json", 1 * KiB)
    put(root / "models/llama-mini/tokenizer.json", 2 * MiB)

    for index in range(60):
        put(root / f"projects/webapp/src/components/Widget{index:02d}.tsx", jitter(rng, 6 * KiB))
    for index in range(30):
        put(root / f"projects/webapp/src/lib/util{index:02d}.ts", jitter(rng, 4 * KiB))
    for index in range(10):
        put(root / f"projects/webapp/src/styles/s{index:02d}.css", jitter(rng, 3 * KiB))
    put(root / "projects/webapp/package.json", 2 * KiB)
    put(root / "projects/webapp/package-lock.json", 900 * KiB)
    for index in range(1, 8):
        put(root / f"projects/thesis/chapters/ch{index}.tex", jitter(rng, 60 * KiB))
    for index in range(24):
        put(root / f"projects/thesis/figures/fig{index:02d}.png", jitter(rng, 450 * KiB))
    put(root / "projects/thesis/thesis.pdf", 4 * MiB)
    put(root / "projects/thesis/refs.bib", 120 * KiB)
    for index in range(12):
        put(root / f"projects/analysis/src/step{index:02d}.py", jitter(rng, 8 * KiB))
    for index in range(6):
        put(root / f"projects/analysis/notebooks/explore{index}.ipynb", jitter(rng, 900 * KiB))
    build_venv(root / "projects/analysis/.venv", rng)
    for index in range(20):
        put(root / f"projects/legacy/src/mod{index:02d}.c", jitter(rng, 20 * KiB))
    put(root / "projects/legacy/secrets/api-keys.env", 2 * KiB)
    put(root / "projects/legacy/secrets/deploy.key", 3 * KiB)

    for year, count in (("2023", 14), ("2024", 16), ("2025", 10)):
        for index in range(count):
            put(root / f"papers/{year}/paper-{index:02d}.pdf", jitter(rng, 2 * MiB, 0.5))
    put(root / "papers/reading-list.md", 12 * KiB)
    put(root / "papers/drafts/workshop-draft-v1.pdf", 1.4 * MiB)
    put(root / "papers/drafts/workshop-draft-v2.pdf", 1.6 * MiB)
    put(root / "archives/2022-projects.tar.gz", 18 * MiB)
    put(root / "archives/old-laptop.zip", 12 * MiB)
    put(root / "tmp/build.log", 250 * KiB)
    put(root / "tmp/pytest-run.out", 400 * KiB)
    put(root / "tmp/profile.prof", 3.5 * MiB)
    for index in range(30):
        put(root / f"notes/2026-{index:02d}.md", jitter(rng, 10 * KiB))
    for name in ("sync.sh", "backup.sh", "mkenv.sh", "gpu-watch.sh", "rsync-ess.sh"):
        put(root / f"bin/{name}", 1 * KiB)

    put(root / "private/journal.md", 40 * KiB)
    put(root / "private/vault.kdbx", 200 * KiB)
    for relative in LOCKED:
        os.chmod(root / relative, 0)


def apply_day(root: Path, day: int, rng: random.Random) -> None:
    """Move the tree to its state at the end of `day` (2..DAYS)."""
    append(root / "tmp/build.log", 250 * KiB)
    if day % 3 == 0:
        put(root / f"models/checkpoints/step-{day * 1000:06d}.pt", 5 * MiB)
    if day in (3, 9, 17, 26):
        raw = root / "datasets/climate/raw"
        start = len(list(raw.iterdir()))
        for index in range(start, start + 12):
            put(raw / f"station-{index:03d}.csv", jitter(rng, 2.7 * MiB, 0.1))
    if day == 8:
        build_node_modules(root / "projects/webapp/node_modules", rng)
    if day == 12:
        for index in range(80, 120):
            put(root / f"photos/2025/IMG_{index:04d}.jpg", jitter(rng, 400 * KiB))
    if day == 15:
        put(root / "archives/home-backup-2026-08.tar.gz", 25 * MiB)
    if day == 24:
        (root / "archives/home-backup-2026-08.tar.gz").unlink()
    if day == 20:
        shutil.rmtree(root / ".cache/pip")
    if day == 27:
        build_pip_cache(root / ".cache/pip", rng)
    if day == 28:
        # More of the image allocated; its logical size does not move.
        with open(root / "vm/dev-box.qcow2", "r+b") as handle:
            for offset in range(4, 512, 16):
                handle.seek(offset * MiB)
                handle.write(_BLOCK)
    if day % 7 not in (0, 6):
        thesis = root / "projects/thesis/thesis.pdf"
        put(thesis, thesis.stat().st_size + 90 * KiB)
    if day == DAYS:
        # Growth, shrink, new and removed in one interval, so Diff's default
        # pair has something of every colour in it.
        thesis = root / "projects/thesis/thesis.pdf"
        put(thesis, thesis.stat().st_size + 1.7 * MiB)
        shutil.rmtree(root / ".cache/huggingface/hub/models--t5-small")
        put(root / "datasets/climate/processed/2026-09.parquet", 6 * MiB)
        shutil.rmtree(root / "papers/drafts")
        put(root / "downloads/ubuntu-24.04-server.iso", 9 * MiB)
        put(root / "downloads/slides-final.pdf", 3 * MiB)
        put(root / "downloads/dataset-v2.zip", 4 * MiB)


def after_last_snapshot(root: Path, rng: random.Random) -> None:
    """What the live scan sees and the newest snapshot does not."""
    for name in ("2026-06", "2026-07", "stations", "anomalies"):
        put(root / f"datasets/climate/processed/{name}.parquet", jitter(rng, 7 * MiB, 0.2))
    append(root / "tmp/build.log", 1 * MiB)
    for index in range(120, 126):
        put(root / f"photos/2025/IMG_{index:04d}.jpg", jitter(rng, 400 * KiB))
    settle(root / "tmp/build.log")


#: The newest snapshot. Fixed rather than "three hours ago", so a reshoot on
#: another day prints the same dates the post quotes.
LAST_SNAPSHOT = datetime(2026, 9, 12, 21, 5, tzinfo=timezone.utc)


def snapshot_times(last: datetime) -> dict[int, list[datetime]]:
    """When each day was sampled: once, except for two days sampled in bursts.

    The bursts are what give the Retention tab something to decide: a run of
    snapshots inside one hour is rolled into its newest by the hourly tier,
    and two on the oldest day by the daily one.
    """
    times = {day: [last - timedelta(days=DAYS - day)] for day in range(1, DAYS + 1)}
    first = times[1][0]
    extra = first - timedelta(minutes=40)
    if extra.date() != first.date():
        extra = first + timedelta(minutes=40)
    times[1] = sorted([first, extra])
    tenth = times[10][0]
    times[10] = [tenth, tenth + timedelta(minutes=20), tenth + timedelta(minutes=40)]
    return times


def disktide(env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        [sys.executable, "-m", "disktide", *args],
        env=env, check=True, capture_output=True, text=True,
    ).stdout


def build_history(env: dict[str, str]) -> None:
    """Replay the month into the monitor's history, back-dating every save."""
    from disktide.domain.metrics import MetricId
    from disktide.domain.policy import ScanPolicy
    from disktide.domain.scan import ScanRequest
    from disktide.domain.snapshot import Snapshot
    from disktide.repositories import default_snapshot_repository
    from disktide.services.scan import ScanService

    rng = random.Random(31)
    times = snapshot_times(LAST_SNAPSHOT)
    repository = default_snapshot_repository()
    repository.connect()
    definition = repository.list_monitors()[0]
    policy, service = ScanPolicy(), ScanService()
    pin_at = times[23][0]
    try:
        for day in range(1, DAYS + 1):
            if day > 1:
                apply_day(STAGE, day, rng)
            for when in times[day]:
                run = service.execute(service.create_run(ScanRequest(
                    path=str(STAGE), metric=MetricId.LOGICAL, policy=policy,
                    source="monitor",
                )))
                snapshot = Snapshot.from_scan_run(
                    run, monitor_id=definition.id,
                    monitor_revision=definition.revision,
                )
                snapshot.timestamp = snapshot.created_at = when
                snapshot.started_at = snapshot.finished_at = when
                repository.save_snapshot(snapshot, run.root)
        after_last_snapshot(STAGE, rng)
        saved = repository.list_snapshots(str(STAGE), strict_path=True, limit=0)
        pinned = next(item.id for item in saved if item.timestamp == pin_at)
    finally:
        repository.close()
    disktide(env, "monitor", "pin", str(pinned), "--label", "before-cleanup")
    print(f"  {len(saved)} snapshots, #{pinned} pinned")


def unlock_and_remove_stage() -> None:
    for relative in LOCKED:
        if (STAGE / relative).exists():
            os.chmod(STAGE / relative, 0o755)
    shutil.rmtree(STAGE, ignore_errors=True)


# --------------------------------------------------------------------------
# driving the app
# --------------------------------------------------------------------------

#: tmux keeps a variation selector in the cell of the glyph before it; the
#: rasterizer would give it a column of its own and shift the rest of the row.
ZERO_WIDTH = str.maketrans("", "", "\ufe0e\ufe0f\u200d")


def start_app(env: dict[str, str]) -> None:
    shots.tmux("kill-server")
    time.sleep(0.5)
    shots.tmux("new-session", "-d", "-s", "cap", "-x", str(COLS), "-y", str(ROWS),
               "bash --norc --noprofile")
    shots.tmux("set", "-g", "window-size", "manual")
    shots.tmux("resize-window", "-t", "cap", "-x", str(COLS), "-y", str(ROWS))
    size = shots.tmux("display", "-pt", "cap", "#{pane_width}x#{pane_height}").strip()
    if size != f"{COLS}x{ROWS}":
        raise RuntimeError(f"tmux pane is {size}, expected {COLS}x{ROWS}")
    exports = " ".join(
        f"{name}={env[name]}"
        for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                     "XDG_STATE_HOME")
    )
    shots.tmux("send-keys", "-t", "cap",
               f"clear; env {exports} COLORTERM=truecolor TERM=xterm-256color "
               f"{sys.executable} {Path(__file__).resolve()} --serve-app {STAGE}", "Enter")


def keys(*names: str, pause: float = 0.15) -> None:
    for name in names:
        shots.tmux("send-keys", "-t", "cap", name)
        time.sleep(pause)


def mouse(action: str, x: int, y: int) -> None:
    """An SGR mouse report at 0-based cell (x, y), typed into the pane."""
    events = {"move": [(35, "M")], "click": [(0, "M"), (0, "m")]}[action]
    for button, final in events:
        report = f"\x1b[<{button};{x + 1};{y + 1}{final}"
        shots.tmux("send-keys", "-t", "cap", "-H", *(f"{ord(c):02x}" for c in report))
        time.sleep(0.08)


def hover(x: int, y: int) -> None:
    # One report arms nothing reliably; the second is the move Textual acts on.
    mouse("move", x - 1, y)
    time.sleep(0.3)
    mouse("move", x, y)
    time.sleep(1.5)


def screen() -> list[str]:
    return shots.screen_text().translate(ZERO_WIDTH).splitlines()


def column(line: str, index: int) -> int:
    return sum(shots.cell_width(char) for char in line[:index])


def find(lines: list[str], needle: str, *, nth: int = 0, min_col: int = 0,
         within: str | None = None, regex: bool = False) -> tuple[int, int]:
    """(cell column, row) of the nth match, searching left of nothing below min_col."""
    seen = 0
    for row, line in enumerate(lines):
        if within is not None and within not in line:
            continue
        pattern = needle if regex else re.escape(needle)
        for match in re.finditer(pattern, line):
            col = column(line, match.start())
            if col < min_col:
                continue
            if seen == nth:
                return col, row
            seen += 1
    raise RuntimeError(f"{needle!r} is not on screen:\n" + "\n".join(lines))


def cursor_to_top() -> None:
    keys(*["Up"] * 24, pause=0.03)
    time.sleep(0.8)


def cursor_down_to(name: str) -> None:
    """Move the tree cursor from the root row to the row named `name`."""
    lines = screen()
    _, root_row = find(lines, "▼ ")
    _, row = find(lines, f"▶ {name}")
    keys(*["Down"] * (row - root_row), pause=0.05)
    time.sleep(0.8)


def photograph(out: Path, captures: Path, name: str, *needles: str,
               settle_first: float = 0.0, pins=()) -> list[str]:
    if settle_first:
        time.sleep(settle_first)
    shots.wait_for(*needles, timeout=180)
    time.sleep(0.4)
    source = captures / f"{name}.ans"
    shots.capture(source)
    source.write_text(source.read_text(encoding="utf-8").translate(ZERO_WIDTH),
                      encoding="utf-8")
    lines = screen()
    target = out / f"{name}.png"
    shots.rasterize(source, target)
    if pins:
        annotate(target, lines, pins)
    print(f"  {target.name}")
    return lines


# --------------------------------------------------------------------------
# a made-up server for FS Overview
# --------------------------------------------------------------------------

# FS Overview draws the machine it runs on, and on a shared cluster that is
# other groups' project mounts, your quota and the drive models -- none of
# which belongs in a public post. So the screen is fed this server instead:
# an encrypted NVMe root, a RAID of spinning disks with a container runtime's
# volumes bound out of it, a btrfs backup disk, NFS home and project shares, a
# Lustre scratch, eight snap images and a new disk nobody has formatted yet.
# Every name is invented, and the addresses come from 192.0.2.0/24, which is
# reserved for documentation.
#
# Only the data is made up. The TUI is started through `--serve-app`, which
# swaps the platform adapter, `statvfs` and `quota` inside
# `disktide.screens.fs_overview` as that module is imported. Nothing else in
# the app sees the fake: the Explorer and Monitor figures scan /tmp/home for
# real.

GiB, TiB = 1024 ** 3, 1024 ** 4

_NFS_OPTIONS = (
    "rw,relatime,vers=4.2,rsize=1048576,wsize=1048576,namlen=255,hard,"
    "proto=tcp,timeo=600,retrans=2,sec=krb5p,clientaddr=192.0.2.15,"
    "local_lock=none,addr=192.0.2.10"
)
_DATA_OPTIONS = "rw,relatime,stripe=256"
_DATA_SPACE = (7.27 * TiB, 6.31 * TiB, 0.5965 * TiB, (244_195_328, 231_870_512))

FAKE_SNAPS: tuple[tuple[str, float], ...] = (
    ("bare/5", 128 * KiB),
    ("core22/1621", 74.3 * MiB),
    ("core24/490", 66.8 * MiB),
    ("firefox/5134", 275.3 * MiB),
    ("gnome-42-2204/176", 505.1 * MiB),
    ("gtk-common-themes/1535", 91.7 * MiB),
    ("snapd/21759", 38.8 * MiB),
    ("thunderbird/524", 181.4 * MiB),
)

#: mountpoint, device, fstype, options, root, (total, used, available,
#: (inodes, free inodes)), block size, rotational. Whatever `total` leaves
#: after `used` and `available` is the root reservation.
FAKE_MOUNTS: tuple[tuple, ...] = (
    ("/", "/dev/mapper/cryptroot", "xfs",
     "rw,relatime,attr2,inode64,logbufs=8,logbsize=32k,noquota", "/",
     (1.82 * TiB, 0.74 * TiB, 1.08 * TiB, (195_318_720, 187_402_113)), 4096, False),
    ("/boot", "/dev/nvme0n1p2", "ext4", "rw,relatime", "/",
     (0.98 * GiB, 0.23 * GiB, 0.70 * GiB, (65_536, 65_121)), 4096, False),
    ("/boot/efi", "/dev/nvme0n1p1", "vfat",
     "rw,relatime,fmask=0077,dmask=0077,codepage=437,iocharset=ascii,"
     "shortname=mixed,errors=remount-ro", "/",
     (0.50 * GiB, 0.03 * GiB, 0.47 * GiB, (0, 0)), 4096, False),
    ("/data", "/dev/md0", "ext4", _DATA_OPTIONS, "/", _DATA_SPACE, 4096, True),
    ("/backup", "/dev/sdc1", "btrfs",
     "rw,relatime,compress=zstd:3,space_cache=v2,subvolid=5,subvol=/", "/",
     (3.64 * TiB, 2.31 * TiB, 1.33 * TiB, (0, 0)), 4096, True),
    ("/home", "fileserver.example.org:/export/home", "nfs4", _NFS_OPTIONS, "/",
     (2.00 * TiB, 1.17 * TiB, 0.83 * TiB, (214_748_364, 171_222_906)),
     1_048_576, None),
    ("/projects", "fileserver.example.org:/export/projects", "nfs4",
     _NFS_OPTIONS, "/",
     (8.00 * TiB, 6.24 * TiB, 1.76 * TiB, (858_993_459, 640_122_338)),
     1_048_576, None),
    ("/scratch", "192.0.2.21@tcp:/scratch", "lustre", "rw,flock,lazystatfs", "/",
     (12.00 * TiB, 3.90 * TiB, 8.10 * TiB, (1_288_490_188, 1_106_239_604)),
     4_194_304, None),
    # A container runtime's named volumes, bound out of the RAID: each one
    # reports all of /data again, which is why the screen folds them.
    *(
        (f"/srv/{name}", "/dev/md0", "ext4", _DATA_OPTIONS, f"/volumes/{name}",
         _DATA_SPACE, 4096, True)
        for name in ("gitlab/data", "gitlab/logs", "grafana", "minio", "postgres")
    ),
    *(
        (f"/snap/{name}", f"/dev/loop{index}", "squashfs",
         "ro,nodev,relatime,errors=continue,threads=single", "/",
         (size, size, 0, (max(29, int(size // 8192)), 0)), 131_072, False)
        for index, (name, size) in enumerate(FAKE_SNAPS)
    ),
)

#: What `lsblk -J -b` would print for the same machine.
FAKE_LSBLK: tuple[dict, ...] = (
    {"name": "nvme0n1", "type": "disk", "size": 2_000_398_934_016,
     "model": "NVMe SSD 2TB", "rota": False, "children": [
         {"name": "nvme0n1p1", "type": "part", "fstype": "vfat",
          "size": 536_870_912, "mountpoint": "/boot/efi", "rota": False},
         {"name": "nvme0n1p2", "type": "part", "fstype": "ext4",
          "size": 1_073_741_824, "mountpoint": "/boot", "rota": False},
         {"name": "nvme0n1p3", "type": "part", "fstype": "crypto_LUKS",
          "size": 1_998_788_034_560, "rota": False, "children": [
              {"name": "cryptroot", "type": "crypt", "fstype": "xfs",
               "size": 1_998_771_257_344, "mountpoint": "/", "rota": False},
          ]},
     ]},
    *(
        {"name": disk, "type": "disk", "size": 8_001_563_222_016,
         "model": "HDD 8TB", "rota": True, "children": [
             {"name": "md0", "type": "raid1", "fstype": "ext4",
              "size": 8_001_427_996_672, "mountpoint": "/data", "rota": True},
         ]}
        for disk in ("sda", "sdb")
    ),
    {"name": "sdc", "type": "disk", "size": 4_000_787_030_016,
     "model": "USB HDD 4TB", "rota": True, "children": [
         {"name": "sdc1", "type": "part", "fstype": "btrfs",
          "size": 4_000_785_841_664, "mountpoint": "/backup", "rota": True},
     ]},
    {"name": "sdd", "type": "disk", "size": 8_001_563_222_016,
     "model": "HDD 8TB", "rota": True},
    *(
        {"name": f"loop{index}", "type": "loop", "fstype": "squashfs",
         "size": int(size), "mountpoint": f"/snap/{name}", "rota": False}
        for index, (name, size) in enumerate(FAKE_SNAPS)
    ),
)

FAKE_QUOTAS = {"/home": (int(38.2 * GiB), 45 * GiB, 50 * GiB)}


def install_made_up_server(module) -> None:
    """Point an imported `disktide.screens.fs_overview` at FAKE_* data."""
    from disktide.collectors.platform import LinuxPlatformAdapter
    from disktide.collectors.platform.base import PlatformAdapter
    from disktide.collectors.platform.models import (
        MountRecord,
        ProbeResult,
        parse_block_node,
    )

    rows = {row[0]: row for row in FAKE_MOUNTS}
    records = [
        MountRecord(device=device, mountpoint=mountpoint,
                    filesystem_type=fstype, options=options, root=root)
        for mountpoint, device, fstype, options, root, *_ in FAKE_MOUNTS
    ]

    class MadeUpServer(LinuxPlatformAdapter):
        def enumerate_mounts(self):
            return ProbeResult.available(records, f"{len(records)} made-up mounts")

        def list_block_devices(self):
            devices = [parse_block_node(node, 0) for node in FAKE_LSBLK]
            return ProbeResult.available(devices, f"{len(devices)} made-up devices")

        def storage_medium(self, path):
            rotational = rows[path][-1] if path in rows else None
            if rotational is None:
                return ProbeResult.degraded(None, "not a local block device")
            return ProbeResult.available(rotational, "made-up disk")

        def detect_transforms(self, device, filesystem_type, mount_options=""):
            found = PlatformAdapter.detect_transforms(
                self, device, filesystem_type, mount_options
            )
            if device.startswith("/dev/md"):
                found.insert(0, "RAID")
            if device.startswith("/dev/mapper/crypt"):
                found.insert(0, "Encrypted")
            return found

        def mount_latency(self, mountpoint):
            return ProbeResult.unavailable("made-up server")

    def statvfs(mountpoint, is_network):
        if mountpoint not in rows:
            return None
        total, used, available, (files, free_files) = rows[mountpoint][5]
        block_size, frsize = rows[mountpoint][6], 4096
        blocks = int(total) // frsize
        free_blocks = blocks - int(used) // frsize
        return os.statvfs_result((
            block_size, frsize, blocks, free_blocks,
            min(free_blocks, int(available) // frsize),
            files, free_files, free_files, 0, 255,
        ))

    module.get_platform_adapter = lambda system=None: MadeUpServer()
    module._statvfs_safe = statvfs
    module._load_user_quotas = lambda: dict(FAKE_QUOTAS)


def serve_app(path: str) -> None:
    """Run DiskTide on `path`, with FS Overview drawing the made-up server.

    The screen module is patched by a one-shot import hook the moment it has
    been imported, rather than by importing it here first: that import
    reaches Textual, and `disktide.__main__` has to pin the colour depth
    before anything imports Textual.
    """
    import importlib.abc
    import importlib.util
    import runpy

    class PatchOnImport(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "disktide.screens.fs_overview":
                return None
            sys.meta_path.remove(self)
            spec = importlib.util.find_spec(fullname)
            execute = spec.loader.exec_module

            def exec_module(module):
                execute(module)
                install_made_up_server(module)

            spec.loader.exec_module = exec_module
            return spec

    sys.meta_path.insert(0, PatchOnImport())
    sys.argv = ["disktide", path]
    runpy.run_module("disktide", run_name="__main__", alter_sys=True)


# --------------------------------------------------------------------------
# pins
# --------------------------------------------------------------------------

def annotate(path: Path, lines: list[str], pins) -> None:
    """Draw numbered pins; each is (number, needle, dx, dy, find-kwargs)."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.open(path).convert("RGB")
    scale = 4
    layer = Image.new("RGBA", (image.width * scale, image.height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = ImageFont.truetype(shots.pick_font(shots.BOLD_FONTS), 15 * scale)
    for number, needle, dx, dy, options in pins:
        col, row = find(lines, needle, **options)
        cx = (shots.PAD + (col + dx) * shots.CELL_W + shots.CELL_W / 2) * scale
        cy = (shots.PAD + (row + dy) * shots.CELL_H + shots.CELL_H / 2) * scale
        r = PIN_RADIUS * scale
        ring = 3 * scale
        draw.ellipse((cx - r - ring, cy - r - ring, cx + r + ring, cy + r + ring),
                     fill=PIN_RING)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=PIN_FILL)
        draw.text((cx, cy + scale), str(number), font=font, fill=PIN_INK, anchor="mm")
    layer = layer.resize(image.size, Image.LANCZOS)
    image.paste(layer, (0, 0), layer)
    image.save(path, optimize=True)


def pin(number: int, needle: str, dx: int = 0, dy: int = 0, **options):
    return (number, needle, dx, dy, options)


# --------------------------------------------------------------------------
# the tour
# --------------------------------------------------------------------------

def explorer_figures(out: Path, captures: Path) -> None:
    chart = {"min_col": TREE_WIDTH}
    photograph(out, captures, "explorer-sunburst",
               "Sunburst [F1]", "t Metric", "by bytes", "latest-dataset", pins=(
        pin(1, "tmp >", dx=16),
        pin(2, "Current", dx=9),
        pin(3, r"[.:\-=+*#]{6}", dx=8, regex=True, within="tmp/"),
        pin(4, "1 hidden", dx=10),
        pin(5, "⚠", dx=3),
        pin(6, "Details [F3]", dx=15),
        pin(7, "1.4 GiB", dx=-3, dy=1, **chart),
        pin(8, "1.4 GiB", dx=-8, dy=-4, **chart),
        pin(9, "vm", dx=1, dy=-2, **chart),
        pin(10, "photos", dx=-4, dy=3, **chart),
        pin(11, "by bytes", dx=11, **chart),
        pin(12, "q Quit", dx=8),
    ))

    cursor_down_to("datasets/")
    lines = screen()
    x, y = find(lines, "datasets", min_col=TREE_WIDTH)
    hover(x + 3, y - 2)
    photograph(out, captures, "sunburst-hover", "MiB ·")
    mouse("move", 2, ROWS - 4)
    time.sleep(1.0)

    cursor_to_top()
    lines = screen()
    x, y = find(lines, "1.4 GiB", min_col=TREE_WIDTH)
    hover(x - 7, y - 1)
    photograph(out, captures, "sunburst-root-ring", "this root")
    mouse("move", 2, ROWS - 4)
    time.sleep(1.0)

    keys("g")
    photograph(out, captures, "sunburst-disc", settle_first=3.0)
    keys("g")
    photograph(out, captures, "sunburst-fill", settle_first=3.0)
    keys("g")
    time.sleep(3.0)

    cursor_down_to("datasets/")
    keys("i")
    photograph(out, captures, "sunburst-drill", "> datasets", "climate/")
    keys("u")
    shots.wait_for("latest-dataset")
    cursor_to_top()

    keys("F2")
    photograph(out, captures, "explorer-treemap", "dev-box.qcow2", pins=(
        pin(1, "home", dx=6, **chart),
        pin(2, "photos", dx=8, **chart),
        pin(3, "llama-mini", dx=12, **chart),
        pin(4, "145 more", nth=1, dx=10, **chart),
        pin(5, "dev-box.qcow2", dx=-3, dy=1, **chart),
        pin(6, "raw", dx=-6, dy=1, **chart),
        pin(7, "projects", dx=12, **chart),
        pin(8, "checkpoints", dx=4, dy=2, **chart),
    ))
    lines = screen()
    x, y = find(lines, "dev-box.qcow2", min_col=TREE_WIDTH)
    hover(x + 4, y + 3)
    photograph(out, captures, "treemap-hover", "512.0 MiB ·")
    mouse("move", 2, ROWS - 4)
    time.sleep(1.0)

    for metric in ("Allocated", "Unique", "Files"):
        keys("t")
        photograph(out, captures, f"metric-{metric.lower()}",
                   f"Metric: {metric}", settle_first=3.0)
    keys("t")
    shots.wait_for("Metric: Logical")
    time.sleep(3.0)

    keys("F3")
    photograph(out, captures, "explorer-details", "Top Items", "Access")

    keys("F1")
    time.sleep(1.0)
    keys("d")
    photograph(out, captures, "diff-sunburst", "Diff snapshots", settle_first=6.0, pins=(
        pin(1, "Diff snapshots", dx=6, dy=-1),
        pin(2, "datasets/", dx=-4),
        pin(3, ".cache/", dx=-4),
        pin(4, "downloads/", dx=-4),
        pin(5, "remainder/", dx=-6),
        pin(6, "▲", dx=-6, **chart, within="MiB (+"),
        pin(7, "growth", dy=-1, **chart),
    ))
    keys("F2")
    photograph(out, captures, "diff-treemap", "remainder", settle_first=1.5)
    keys("d")
    shots.wait_for("Current")
    keys("F1")
    time.sleep(1.5)

    cursor_down_to("projects/")
    keys("M")
    photograph(out, captures, "monitor-editor", "Set up monitoring", settle_first=1.0)
    keys("Escape")
    time.sleep(1.0)
    cursor_to_top()


def monitor_figures(out: Path, captures: Path) -> None:
    keys("2")
    lines = photograph(out, captures, "monitor-trend", "Space-Time Trend",
                       "canonical point(s)", "• home", settle_first=2.0, pins=(
        pin(1, "enabled/no-host", dx=2, dy=2),
        pin(2, "Start sampling", dx=-3),
        pin(3, "Retention", dx=12, within="History"),
        pin(4, "partial confidence", dx=20),
        pin(5, "sampled", dx=9),
        pin(6, "Heatmap [F4]", dx=15),
        pin(7, "*", dy=-1, within="┤"),
        pin(8, "Size (MiB)", dx=11),
        pin(9, "pinned", dx=9, within="present"),
    ))
    tabs = {tab: find(lines, tab, within="History  Details") for tab in
            ("History", "Details", "Retention")}
    keys("F2")
    photograph(out, captures, "monitor-diff-map", "remainder", settle_first=2.0)
    keys("F3")
    photograph(out, captures, "monitor-growth-rings", settle_first=2.0)
    keys("F4")
    photograph(out, captures, "monitor-heatmap", "consistency", settle_first=2.0, pins=(
        pin(1, "Path", dx=8, within="consistency"),
        pin(2, "·····", dx=-3, within="consistency"),
        pin(3, "1111", dx=-8, within="tmp/build.log"),
        pin(4, "????", dx=-3, within="downloads"),
        pin(5, "··1··1", dx=-3, within="models"),
        pin(6, "consistency", dx=13),
        pin(7, "more", dx=5),
    ))
    keys("F1")
    time.sleep(1.5)
    for tab, needle in (("Details", "Lifecycle"), ("Retention", "Policy")):
        x, y = tabs[tab]
        mouse("click", x + 1, y)
        photograph(out, captures, f"monitor-{tab.lower()}", needle, settle_first=2.0)
    x, y = tabs["History"]
    mouse("click", x + 1, y)
    time.sleep(1.5)


def fs_figures(out: Path, captures: Path) -> None:
    keys("3")
    lines = photograph(out, captures, "fs-overview", "Block Devices",
                       "press i to expand", "total capacity",
                       settle_first=3.0, pins=(
        pin(1, "filesystem(s) mounted", dx=-5),
        pin(2, "■", dx=10, dy=-1),
        pin(3, "Storage", dx=19),
        pin(4, "Usage", dx=11, dy=-1),
        pin(5, "Quota", dx=7),
        pin(6, "total capacity", dx=16),
        pin(7, "press i to expand", dx=19),
    ))
    _, header = find(lines, "FS Type")
    _, data = find(lines, "/data ")
    keys(*["Down"] * (data - header - 1), pause=0.1)
    keys("Enter")
    photograph(out, captures, "fs-mount-details", "Mount Options", settle_first=2.0)
    keys("Escape")
    time.sleep(1.0)
    keys("Tab")
    time.sleep(0.5)
    keys("Enter")
    photograph(out, captures, "fs-block-details", "Partitions", settle_first=2.0)
    keys("Escape")
    time.sleep(1.0)


# --------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Regenerate the TUI field guide's figures from the real TUI.",
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT,
        help="directory the PNGs are written to (default: %(default)s, "
             "the assets checkout next to this repository)",
    )
    args = parser.parse_args(argv)
    if not args.out.is_dir():
        parser.error(
            f"--out {args.out} is not a directory; clone "
            "github.com/williamqwu/assets next to this repository, or pass --out"
        )
    return args


def main() -> None:
    if sys.argv[1:2] == ["--serve-app"]:
        serve_app(sys.argv[2])
        return
    out = parse_args().out
    if shutil.which("tmux") is None:
        raise SystemExit("tmux is required")
    shots.SOCKET = SOCKET
    shots.COLS, shots.ROWS = COLS, ROWS
    unlock_and_remove_stage()
    scratchguard.claim(STAGE, entries=STAGE_ENTRIES, label="blog-stage")
    scratch = Path(tempfile.mkdtemp(
        dir=scratchguard.scratch_dir("blog-shots", entries=256)
    ))
    captures = scratch / "captures"
    captures.mkdir()
    xdg = {
        f"XDG_{kind}_HOME": str(scratch / kind.lower())
        for kind in ("CONFIG", "DATA", "CACHE", "STATE")
    }
    for path in xdg.values():
        Path(path).mkdir()
    os.environ.update(xdg)
    env = dict(os.environ)

    try:
        print(f"staging {STAGE} and replaying {DAYS} days into {scratch}")
        build_day_one(STAGE)
        disktide(env, "monitor", "add", str(STAGE), "--label", "home",
                 "--interval", "24h")
        build_history(env)

        print(f"driving DiskTide at {COLS}x{ROWS}")
        start_app(env)
        explorer_figures(out, captures)
        monitor_figures(out, captures)
        fs_figures(out, captures)
    finally:
        shots.tmux("kill-server")
        unlock_and_remove_stage()
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
