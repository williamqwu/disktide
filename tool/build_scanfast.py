#!/usr/bin/env python3
"""Build the optional scanner extension in place, for a development checkout.

`hatch_build.py` compiles `src/disktide/scanner/_scanfast.c` for wheel and
editable builds. That covers `pip install .` and `pip install -e .` -- but
not the moment a developer *edits the C file*, because nothing reruns the
hook until the next install. This script is that rerun: it compiles the
extension straight into `src/disktide/scanner/`, next to its source, where
the editable install's path entry finds it.

It deliberately makes the same three decisions the build hook makes, and for
the same reasons:

* **The compiler is looked for by name before it is run.** `sysconfig`'s
  `CC` is whatever built the interpreter, which on a uv-managed CPython is
  `clang` -- a name plenty of Linux boxes do not have. `$CC`, then
  sysconfig's, then plain `cc`, `gcc`, `clang`: the first one on `PATH`.
* **A macOS extension is a bundle**, not a shared library
  (`-bundle -undefined dynamic_lookup`): the Python symbols it calls are
  resolved by the interpreter that loads it and are in no library at build
  time, so `-shared` fails to link there.
* **The object is loaded before it is believed.** A compiler that exits 0
  and leaves something the interpreter cannot import -- a wrong ABI, a stale
  `Python.h`, a `$CC` that is not a C compiler for this interpreter -- used
  to show up only as `disktide doctor` quietly reporting the fallback. The
  check runs in a subprocess, because an extension loaded into this one
  cannot be unloaded again.

Usage:
    python tool/build_scanfast.py               # build in place, then verify
    python tool/build_scanfast.py --print-command
    python tool/build_scanfast.py --output DIR  # build somewhere else
    python tool/build_scanfast.py --dry-run     # resolve everything, build
                                                # nothing

Exits non-zero when there is no compiler, the compile fails, or the object
it produced does not import. Delete the `.so` (it is gitignored) or set
`DISKTIDE_ACCEL=0` to go back to the pure-Python reader.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "src" / "disktide" / "scanner" / "_scanfast.c"


def compiler_candidates() -> list[str]:
    """In preference order, the same list `hatch_build.py` uses."""
    candidates = [
        os.environ.get("CC"),
        sysconfig.get_config_var("CC"),
        "cc",
        "gcc",
        "clang",
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return ordered


def resolve_compiler() -> list[str] | None:
    """The first candidate whose executable is actually on PATH."""
    for candidate in compiler_candidates():
        argv = candidate.split()
        if argv and shutil.which(argv[0]) is not None:
            return argv
    return None


def build_command(compiler: list[str], output: Path) -> list[str]:
    link = (
        ["-bundle", "-undefined", "dynamic_lookup"]
        if sys.platform == "darwin"
        else ["-shared"]
    )
    return [
        *compiler,
        "-O2",
        "-Wall",
        *link,
        "-fPIC",
        "-I" + sysconfig.get_paths()["include"],
        "-o",
        str(output),
        str(SOURCE),
    ]


def import_failure(path: Path) -> str | None:
    """Why the freshly built object would not load, or None if it does.

    By file location rather than by name: what is being tested is the
    object, not whether `disktide` happens to be importable from here.
    """
    script = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('_scanfast', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "module.scan_dir\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if result.returncode == 0:
        return None
    lines = (result.stderr or "").strip().splitlines()
    return lines[-1] if lines else f"exit status {result.returncode}"


def report_backend() -> None:
    """What `disktide.scanner.accel` picks now, asked in a fresh process.

    A fresh one because `accel` decides once, at import, and this process
    may already have decided before the object existed.
    """
    script = (
        "from disktide.scanner.accel import ACCEL_BACKEND, ACCEL_REASON\n"
        "print(f'{ACCEL_BACKEND}: {ACCEL_REASON}')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    if result.returncode == 0:
        print(f"scanfast: accel reports {result.stdout.strip()}")
    else:
        # Not a failure of the build: the package around the object may not
        # be importable from wherever this was run.
        print("scanfast: could not ask accel which reader it picked")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Compile disktide's optional scanner extension in place."
    )
    parser.add_argument(
        "--output",
        metavar="DIR",
        default=None,
        help="write the object here instead of next to _scanfast.c",
    )
    parser.add_argument(
        "--print-command",
        action="store_true",
        help="print the compiler invocation and exit without running it",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve the compiler and the output path, but compile nothing",
    )
    args = parser.parse_args(argv)

    if not SOURCE.exists():
        print(f"scanfast: {SOURCE} is missing", file=sys.stderr)
        return 1

    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    directory = Path(args.output) if args.output else SOURCE.parent
    output = directory / ("_scanfast" + suffix)

    compiler = resolve_compiler()
    if compiler is None:
        print(
            "scanfast: no C compiler on PATH (tried "
            f"{', '.join(compiler_candidates())})",
            file=sys.stderr,
        )
        return 1

    command = build_command(compiler, output)
    if args.print_command:
        print(" ".join(command))
        return 0
    if args.dry_run:
        print(f"scanfast: would build {output} with {compiler[0]}")
        print(" ".join(command))
        return 0

    directory.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        print(
            f"scanfast: {compiler[0]} failed\n{(exc.stderr or '').strip()}",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"scanfast: {compiler[0]} could not be run ({exc})", file=sys.stderr)
        return 1

    failure = import_failure(output)
    if failure is not None:
        print(
            f"scanfast: {compiler[0]} succeeded but the object it produced "
            f"does not import ({failure})",
            file=sys.stderr,
        )
        return 1

    print(f"scanfast: built {output} with {compiler[0]}")
    if args.output is None:
        report_backend()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
