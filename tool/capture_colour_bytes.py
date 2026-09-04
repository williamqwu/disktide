#!/usr/bin/env python3
"""What the terminal actually receives, read back off a real tmux pane.

Every other check on the ANSI theme reads the objects the renderer builds.
This one reads bytes: it drives the real TUI in a real tmux server and
pulls the pane back with ``capture-pane -e``, which re-emits the colours
tmux *stored*, so a truecolor SGR the app wrote comes back as a truecolor
SGR whatever the (absent) client could have coped with.

That is exactly why it cannot show the downgrade -- tmux quantises at
output time, per client, and there is no client -- and exactly why it can
show the thing that matters: whether the ANSI theme wrote anything for a
downgrade to get hold of in the first place. A single ``rgb()`` left in a
table renders perfectly here and arrives at a web shell as somebody else's
nearest match.

Two runs, because a check that only ever sees the passing case does not
prove it can fail:

  ansi       DISKTIDE_COLOR_DEPTH=16, so the resolver picks the ANSI theme.
             Expect no 38;2/48;2/38;5/48;5 at all.
  truecolor  DISKTIDE_COLOR_DEPTH=truecolor and the shipped palette.
             Expect 38;2 and 48;2, which is what makes the first result
             mean something.

Everything is disposable and nothing touches the user's world: a private
tmux server (``-L``), a scratch tree, and XDG roots pointed at a temporary
directory, because the explorer saves its config on some key presses.

    python tool/capture_colour_bytes.py [--keep] [--server NAME]
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WIDTH, HEIGHT = 200, 50

# The SGR forms that are not sixteen-colour: 24-bit and 256-indexed, as
# foreground and as background.
DEEP = ("38;2;", "48;2;", "38;5;", "48;5;")

# What a 16-colour terminal is allowed to be asked for.
SIXTEEN = re.compile(r"^(3[0-7]|4[0-7]|9[0-7]|10[0-7])$")

# A tree with something in every category, small enough that the scan is
# over before the first poll comes back.
TREE = {
    "src": [("app.py", 90_000), ("util.py", 41_000)],
    "docs": [("manual.md", 55_000)],
    "data": [("train.parquet", 220_000)],
    "media": [("hero.png", 140_000)],
    "build": [("core.o", 130_000), ("run.log", 70_000)],
    "arch": [("bundle.zip", 60_000)],
    "misc": [("thing.xyz", 30_000)],
}


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
        folder.mkdir(parents=True)
        for filename, size in files:
            (folder / filename).write_bytes(b"x" * size)
    return tree


def wait_for(server: str, target: str, needle: str, deadline: float) -> str:
    """Poll the pane's plain text until *needle* shows up."""
    end = time.monotonic() + deadline
    pane = ""
    while time.monotonic() < end:
        pane = tmux(server, "capture-pane", "-p", "-t", target, check=False)
        if needle in pane:
            return pane
        time.sleep(0.25)
    raise SystemExit(
        f"never saw {needle!r} in the pane within {deadline}s; last capture:\n"
        + pane
    )


def run_case(
    server: str, scratch: Path, tree: Path, label: str, env: dict[str, str]
) -> str:
    """Launch the TUI once and return the pane with its escape codes."""
    window = f"case-{label}"
    exports = " ".join(f"{key}={value}" for key, value in sorted(env.items()))
    python = Path(sys.executable)
    command = (
        f"cd {tree} && "
        f"XDG_CONFIG_HOME={scratch}/config XDG_DATA_HOME={scratch}/data "
        f"XDG_CACHE_HOME={scratch}/cache XDG_STATE_HOME={scratch}/state "
        f"{exports} {python} -m disktide"
    )
    tmux(
        server, "new-window", "-d", "-n", window,
        "-e", f"PYTHONPATH={Path(__file__).resolve().parents[1] / 'src'}",
        f"sh -c '{command}'",
    )
    target = f"{window}.0"
    # The welcome screen comes up first with the cwd in its path input.
    wait_for(server, target, "Enter", 60.0)
    tmux(server, "send-keys", "-t", target, "Enter")
    # The footer only exists on the explorer, and the legend only after
    # the scan has categorised something.
    wait_for(server, target, "Quit", 60.0)
    wait_for(server, target, "code", 60.0)
    time.sleep(1.0)
    pane = tmux(server, "capture-pane", "-e", "-p", "-t", target)
    tmux(server, "kill-window", "-t", window, check=False)
    return pane


def report(label: str, pane: str) -> dict[str, int]:
    counts = {sequence: pane.count(sequence) for sequence in DEEP}
    codes: set[str] = set()
    for match in re.findall(r"\x1b\[([0-9;]*)m", pane):
        codes.update(part for part in match.split(";") if part)
    colour_codes = sorted(
        (code for code in codes if SIXTEEN.match(code)), key=int
    )
    other = sorted((code for code in codes if not SIXTEEN.match(code)), key=int)
    print(f"--- {label} ---")
    print("  " + "  ".join(f"{k} {v}" for k, v in counts.items()))
    print(f"  16-colour SGRs used: {' '.join(colour_codes) or 'none'}")
    print(f"  other SGR parameters: {' '.join(other) or 'none'}")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="cap", help="private tmux socket name")
    parser.add_argument("--keep", action="store_true", help="keep the scratch tree")
    args = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="disktide-colour-"))
    tree = make_tree(scratch)
    tmux(
        args.server, "new-session", "-d", "-s", "base",
        "-x", str(WIDTH), "-y", str(HEIGHT), "sh",
    )
    print(f"tmux -L {args.server}, {WIDTH}x{HEIGHT}, tree {tree}")
    print("TERM inside:", tmux(args.server, "show", "-gv", "default-terminal").strip())
    try:
        ansi = run_case(
            args.server, scratch, tree, "ansi",
            {"DISKTIDE_COLOR_DEPTH": "16"},
        )
        deep = run_case(
            args.server, scratch, tree, "truecolor",
            {"DISKTIDE_COLOR_DEPTH": "truecolor"},
        )
    finally:
        tmux(args.server, "kill-server", check=False)
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)

    ansi_counts = report("ansi (DISKTIDE_COLOR_DEPTH=16)", ansi)
    deep_counts = report("shipped palette (DISKTIDE_COLOR_DEPTH=truecolor)", deep)

    failures = []
    if any(ansi_counts.values()):
        failures.append(
            "the ANSI run emitted a deep-colour SGR: "
            + ", ".join(f"{k}x{v}" for k, v in ansi_counts.items() if v)
        )
    if not (deep_counts["38;2;"] or deep_counts["48;2;"]):
        failures.append(
            "the control run emitted no 24-bit SGR at all, so the first "
            "result proves nothing"
        )
    print()
    for line in failures:
        print("FAIL:", line)
    if not failures:
        print("PASS: the ANSI theme wrote nothing for a downgrade to reach, "
              "and the shipped palette wrote plenty.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
