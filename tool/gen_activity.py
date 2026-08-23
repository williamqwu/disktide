#!/usr/bin/env python3
"""Generate random filesystem activity for testing sizetrail.

Creates, modifies, and deletes small files continuously under a target
directory so you can observe the monitor/watch features in action.

Usage:
    python tool/gen_activity.py                  # default ~/tests/sysmonitor-cli
    python tool/gen_activity.py /tmp/testdir     # custom path
    python tool/gen_activity.py --interval 2     # seconds between actions
    python tool/gen_activity.py --batch 5        # files per cycle
    python tool/gen_activity.py --max-files 500  # cap at 500 files
    python tool/gen_activity.py --max-size 50M   # cap total size at 50 MB
"""

from __future__ import annotations

import argparse
import os
import random
import string
import sys
import time
from pathlib import Path

DEFAULT_TARGET = os.path.expanduser("~/tests/sysmonitor-cli")
DEFAULT_MAX_FILES = 1000
DEFAULT_MAX_SIZE = "100M"

_SIZE_UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3}


def parse_size(value: str) -> int:
    """Parse a size string like '50M', '1G', '512K' into bytes."""
    value = value.strip()
    unit = value[-1].lower()
    if unit in _SIZE_UNITS:
        return int(value[:-1]) * _SIZE_UNITS[unit]
    return int(value)


def format_size(n: int) -> str:
    for unit, threshold in [("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)]:
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{n} B"


def measure(root: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) under root."""
    count = 0
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            count += 1
            total += p.stat().st_size
    return count, total

SUBDIRS = [
    "logs", "cache", "data", "tmp", "uploads",
    "logs/archive", "cache/thumbnails", "data/exports",
]


def random_name(ext: str = ".txt") -> str:
    stem = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return stem + ext


def random_content(min_bytes: int = 64, max_bytes: int = 8192) -> bytes:
    size = random.randint(min_bytes, max_bytes)
    return os.urandom(size)


def create_file(root: Path) -> str:
    subdir = root / random.choice(SUBDIRS)
    subdir.mkdir(parents=True, exist_ok=True)
    ext = random.choice([".txt", ".log", ".csv", ".json", ".tmp", ".dat"])
    path = subdir / random_name(ext)
    path.write_bytes(random_content())
    return str(path)


def modify_file(root: Path) -> str | None:
    files = [p for p in root.rglob("*") if p.is_file()]
    if not files:
        return None
    target = random.choice(files)
    with open(target, "ab") as f:
        f.write(random_content(32, 2048))
    return str(target)


def delete_file(root: Path) -> str | None:
    files = [p for p in root.rglob("*") if p.is_file()]
    if not files:
        return None
    target = random.choice(files)
    target.unlink()
    # Remove empty parent dirs up to root
    parent = target.parent
    while parent != root:
        try:
            parent.rmdir()
            parent = parent.parent
        except OSError:
            break
    return str(target)


def create_dir(root: Path) -> str:
    depth = random.randint(1, 3)
    parts = [
        "".join(random.choices(string.ascii_lowercase, k=5))
        for _ in range(depth)
    ]
    new_dir = root / os.path.join(*parts)
    new_dir.mkdir(parents=True, exist_ok=True)
    return str(new_dir)


def run_cycle(
    root: Path, batch: int, max_files: int, max_bytes: int,
) -> list[str]:
    file_count, total_bytes = measure(root)
    at_file_limit = file_count >= max_files
    at_size_limit = total_bytes >= max_bytes

    actions = []
    for _ in range(batch):
        roll = random.random()

        if at_file_limit or at_size_limit:
            # Over limit: only delete or modify (shrink-biased)
            if roll < 0.6:
                path = delete_file(root)
                if path:
                    actions.append(f"  - {path}")
            else:
                path = modify_file(root)
                if path:
                    actions.append(f"  ~ {path}")
        elif roll < 0.45:
            path = create_file(root)
            actions.append(f"  + {path}")
        elif roll < 0.70:
            path = modify_file(root)
            if path:
                actions.append(f"  ~ {path}")
        elif roll < 0.85:
            path = delete_file(root)
            if path:
                actions.append(f"  - {path}")
        else:
            path = create_dir(root)
            actions.append(f"  d {path}")
    return actions


def confirm_existing(target: Path) -> bool:
    entries = list(target.iterdir()) if target.exists() else []
    if not entries:
        return True
    print(f"\nWARNING: {target} already exists with {len(entries)} entries:")
    for entry in entries[:10]:
        print(f"  {entry.name}{'/' if entry.is_dir() else ''}")
    if len(entries) > 10:
        print(f"  ... and {len(entries) - 10} more")
    print()
    answer = input("Continue and modify this directory? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def main():
    parser = argparse.ArgumentParser(
        description="Generate random filesystem activity for testing sizetrail."
    )
    parser.add_argument(
        "path", nargs="?", default=DEFAULT_TARGET,
        help=f"Target directory (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--interval", type=float, default=3.0,
        help="Seconds between cycles (default: 3)",
    )
    parser.add_argument(
        "--batch", type=int, default=3,
        help="Number of actions per cycle (default: 3)",
    )
    parser.add_argument(
        "--max-files", type=int, default=DEFAULT_MAX_FILES,
        help=f"Max number of files (default: {DEFAULT_MAX_FILES})",
    )
    parser.add_argument(
        "--max-size", type=str, default=DEFAULT_MAX_SIZE,
        help=f"Max total size, e.g. 50M, 1G (default: {DEFAULT_MAX_SIZE})",
    )
    args = parser.parse_args()
    max_bytes = parse_size(args.max_size)

    root = Path(args.path).resolve()

    if not confirm_existing(root):
        print("Aborted.")
        sys.exit(0)

    root.mkdir(parents=True, exist_ok=True)
    # Seed subdirectories
    for sub in SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)

    print(f"Generating activity in {root}")
    print(f"  interval={args.interval}s, batch={args.batch}")
    print(f"  max-files={args.max_files}, max-size={format_size(max_bytes)}")
    print("  Ctrl+C to stop\n")

    cycle = 0
    try:
        while True:
            cycle += 1
            actions = run_cycle(root, args.batch, args.max_files, max_bytes)
            file_count, total_bytes = measure(root)
            print(f"[cycle {cycle}] {file_count} files, {format_size(total_bytes)}")
            for a in actions:
                print(a)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        file_count, total_bytes = measure(root)
        print(f"\nStopped after {cycle} cycles. {file_count} files, {format_size(total_bytes)} in {root}")


if __name__ == "__main__":
    main()
