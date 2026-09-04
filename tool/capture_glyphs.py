#!/usr/bin/env python3
"""Which glyphs the app actually puts on screen, read off a real tmux pane.

`tests/test_web_glyphs.py` gates the *styles* -- what the widgets ask for --
because that is host-independent and that is where the eighth blocks came
from. This is the other end: it drives the real TUI in a real tmux server,
walks the screens a user walks, pulls each pane back with ``capture-pane -p``
and reports every non-ASCII glyph that is not on the reviewed list, with the
screen and the row it appeared on.

It catches what a style check cannot: a glyph written into a renderable by
hand, a Rich default, a spinner, a scrollbar thumb end -- anything drawn
below the CSS layer. It cannot catch what only a browser sees; every
terminal here draws `▔` at one cell, which is the whole reason the report is
about *identity* rather than about width.

Nothing touches the user's world: a private tmux server (``-L``), a scratch
tree, and XDG roots under a temporary directory, because the explorer writes
its config on some key presses. The app writes its screen to stderr, so the
pane is never redirected.

    python tool/capture_glyphs.py [--size 307x71] [--size 120x32] [--out DIR]
                                  [--server NAME] [--keep]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from disktide.glyphs import (  # noqa: E402
    FOREIGN_CHROME_GLYPHS,
    UNSAFE_GLYPHS,
    unreviewed_glyphs_in,
    unsafe_glyphs_in,
)

# A tree with enough in it that the explorer draws a chart, a legend and a
# scrollbar, and small enough that the scan is over before the first poll.
TREE = {
    "src": [("app.py", 90_000), ("util.py", 41_000)],
    "docs": [("manual.md", 55_000)],
    "data": [("train.parquet", 220_000)],
    "media": [("hero.png", 140_000)],
    "build": [("core.o", 130_000), ("run.log", 70_000)],
    "arch": [("bundle.zip", 60_000)],
    "misc": [("thing.xyz", 30_000)],
    "src/__pycache__": [("app.cpython-313.pyc", 12_000)],
}

#: (label, keys to send before capturing, text that proves we got there).
#: Applied in order, each on top of the state the previous one left, so a
#: step that silently did nothing shows up as the next step's missing
#: needle rather than as a duplicate capture nobody looks at.
WALK: list[tuple[str, tuple[str, ...], str | None]] = [
    ("welcome", (), "Enter explore"),
    ("explorer", ("Enter",), "Quit"),
    ("keymap", ("?",), "esc closes"),
    ("settings", ("Escape", ","), "System Information"),
    # Again with the focus walked down the form: at 120 columns the
    # selects and switches start below the fold, and a screen the tool
    # never scrolled to is a screen it never checked.
    ("settings-fields", ("Down",) * 10, None),
    ("cleanup", ("Escape", "4"), "Score only ranks opportunities"),
    ("cleanup-modal", ("a", "p"), "Save Preview"),
    ("palette", ("Escape", "Escape", "C-p"), "Search for commands"),
]


def tmux(server: str, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["tmux", "-L", server, *args],
        capture_output=True, text=True, check=False,
    )
    if check and result.returncode != 0:
        raise SystemExit(f"tmux {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def make_tree(root: Path) -> Path:
    tree = root / "tree"
    for name, files in TREE.items():
        folder = tree / name
        folder.mkdir(parents=True, exist_ok=True)
        for filename, size in files:
            (folder / filename).write_bytes(b"x" * size)
    return tree


def seed_config(scratch: Path) -> None:
    """Turn cleanup mode on, so the walk can reach its screen and modal.

    Written through `save_config` under the scratch XDG root rather than by
    hand, so it stays whatever the app reads.
    """
    subprocess.run(
        [
            sys.executable, "-c",
            "from disktide.config import AppConfig, save_config\n"
            "config = AppConfig()\n"
            "config.ui.show_cleanup = True\n"
            "save_config(config)\n",
        ],
        check=True,
        env={
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "XDG_CONFIG_HOME": str(scratch / "config"),
            "XDG_DATA_HOME": str(scratch / "data"),
            "XDG_CACHE_HOME": str(scratch / "cache"),
            "XDG_STATE_HOME": str(scratch / "state"),
        },
    )


def wait_for(server: str, target: str, needle: str, deadline: float) -> None:
    end = time.monotonic() + deadline
    pane = ""
    while time.monotonic() < end:
        pane = tmux(server, "capture-pane", "-p", "-t", target, check=False)
        if needle in pane:
            return
        time.sleep(0.25)
    raise SystemExit(
        f"never saw {needle!r} in the pane within {deadline}s; last capture:\n"
        + pane
    )


def describe(char: str) -> str:
    try:
        name = unicodedata.name(char)
    except ValueError:
        name = "<unnamed>"
    return f"U+{ord(char):04X} {char!r} {name}"


def scan_pane(label: str, pane: str) -> tuple[list[str], list[str], list[str]]:
    """Split a pane's non-ASCII glyphs into the three buckets."""
    unsafe: list[str] = []
    unknown: list[str] = []
    foreign: list[str] = []
    for number, line in enumerate(pane.splitlines(), start=1):
        for char in sorted(unsafe_glyphs_in(line)):
            unsafe.append(f"  {label} row {number}: {describe(char)}")
        for char in sorted(unreviewed_glyphs_in(line)):
            if char in UNSAFE_GLYPHS:
                continue
            where = foreign if char in FOREIGN_CHROME_GLYPHS else unknown
            where.append(f"  {label} row {number}: {describe(char)}")
    return unsafe, unknown, foreign


def run_size(
    server: str, scratch: Path, tree: Path, width: int, height: int, out: Path
) -> tuple[list[str], list[str], list[str]]:
    window = f"g-{width}x{height}"
    command = (
        f"cd {tree} && "
        f"XDG_CONFIG_HOME={scratch}/config XDG_DATA_HOME={scratch}/data "
        f"XDG_CACHE_HOME={scratch}/cache XDG_STATE_HOME={scratch}/state "
        f"{Path(sys.executable)} -m disktide"
    )
    tmux(
        server, "new-window", "-d", "-n", window,
        "-e", f"PYTHONPATH={REPO_ROOT / 'src'}",
        f"sh -c '{command}'",
    )
    target = f"{window}.0"
    # `-x/-y` on `new-window` is advisory; the window has to be resized and
    # the size read back, or the capture is whatever the server felt like.
    tmux(server, "resize-window", "-t", target, "-x", str(width), "-y", str(height))
    actual = tmux(server, "display-message", "-p", "-t", target,
                  "#{pane_width}x#{pane_height}").strip()
    if actual != f"{width}x{height}":
        raise SystemExit(f"asked for {width}x{height}, tmux gave {actual}")

    unsafe: list[str] = []
    unknown: list[str] = []
    foreign: list[str] = []
    for label, keys, needle in WALK:
        for key in keys:
            tmux(server, "send-keys", "-t", target, key)
            time.sleep(0.6)
        if needle is not None:
            wait_for(server, target, needle, 60.0)
        time.sleep(1.5)
        pane = tmux(server, "capture-pane", "-p", "-t", target)
        (out / f"{label}_{width}x{height}.txt").write_text(pane)
        found = scan_pane(f"{label} {width}x{height}", pane)
        unsafe += found[0]
        unknown += found[1]
        foreign += found[2]
        print(f"  {label} {width}x{height}: captured")
    tmux(server, "kill-window", "-t", window, check=False)
    return unsafe, unknown, foreign


def parse_size(text: str) -> tuple[int, int]:
    width, _, height = text.partition("x")
    return int(width), int(height)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="dtglyph", help="private tmux socket")
    parser.add_argument(
        "--size", action="append", default=None,
        help="WIDTHxHEIGHT, repeatable (default: 307x71 and 120x32)",
    )
    parser.add_argument("--out", default=None, help="where to save the panes")
    parser.add_argument("--keep", action="store_true", help="keep the scratch tree")
    args = parser.parse_args()

    sizes = [parse_size(size) for size in (args.size or ["307x71", "120x32"])]
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="dt-glyphs-"))
    out.mkdir(parents=True, exist_ok=True)

    scratch = Path(tempfile.mkdtemp(prefix="disktide-glyphs-"))
    tree = make_tree(scratch)
    seed_config(scratch)
    tmux(args.server, "new-session", "-d", "-s", "base", "-x", "200", "-y", "50", "sh")
    tmux(args.server, "set-option", "-g", "window-size", "manual")
    print(f"tmux -L {args.server}, tree {tree}, panes -> {out}")

    unsafe: list[str] = []
    unknown: list[str] = []
    foreign: list[str] = []
    try:
        for width, height in sizes:
            found = run_size(args.server, scratch, tree, width, height, out)
            unsafe += found[0]
            unknown += found[1]
            foreign += found[2]
    finally:
        tmux(args.server, "kill-server", check=False)
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)

    print()
    if unsafe:
        print(f"FAIL: {len(unsafe)} mis-measured glyph(s) on screen:")
        for line in unsafe:
            print(line)
    if unknown:
        print(f"UNREVIEWED: {len(unknown)} non-ASCII glyph(s) outside the list:")
        for line in unknown:
            print(line)
    if foreign:
        print(f"noted: {len(foreign)} of Textual's own non-WGL4 chrome glyph(s):")
        for line in foreign:
            print(line)
    if not unsafe and not unknown:
        print("PASS: every non-ASCII glyph on every captured screen is reviewed.")
    return 1 if unsafe or unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
