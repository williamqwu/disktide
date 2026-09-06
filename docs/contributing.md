# Contributing

Guide for developers working on disktide.

## Setup

```bash
git clone <repo-url> disktide && cd disktide
uv sync --locked
```

Creates an editable `.venv` from the committed `uv.lock`, including dev
dependencies (pytest, pytest-asyncio, textual-dev).

## Running Tests

```bash
uv run pytest tests/ -v                    # full suite
uv run pytest tests/test_scanner.py -v     # specific module
uv run pytest tests/ --cov=disktide        # with coverage
```

`PathSuggester` coroutine tests use `@pytest.mark.asyncio`. Most Textual app
tests wrap an async helper with `asyncio.run(...)` and `app.run_test()` —
follow the style of the nearby tests.

### Reproducing a CI-only Failure

GitHub runners are two-core ext4 containers. Four host-sensitive failures
have passed locally and failed in CI. `tests/hostshape.py` models those
differences:

```bash
DISKTIDE_HOST_SHAPE=readdir,medium,cores uv run pytest -q -n auto
DISKTIDE_HOST_SHAPE=readdir uv run pytest -q tests/test_scheduler_cursor.py
```

| Shape | Models | Example failure |
|-------|--------|-----------------|
| `readdir` | a directory read returning filesystem order, not creation order — applied to `os.scandir` *and* to the tuples `scheduler.scan_dir` returns, so it reaches the C reader too | Chunked sort window falling back to filesystem order at `entry_chunk_size=1` |
| `medium` | Mount whose rotational bit sysfs can't read → `unknown` medium | Unknown-medium badge holding a raw Rich style instead of an ink role |
| `cores` | Two cores via `os.cpu_count()` and `os.sched_getaffinity()` | Worker-count selection (and, before the duty cycle, the `live_scan_render` auto-gate) |
| `sleepless` | Every `time.sleep` collapsing to nothing | Diagnostic only, not in CI — trips tests that use sleep as an instrument |

Two things to know:

- **Core count is pinned for every test run** (shape or no shape) to
  `DEFAULT_CPUS`. The `cores` shape is how you deliberately test the other
  answer.
- **`taskset` / `--cpus` don't reproduce `cores`** — they change scheduling
  without changing what `os.cpu_count()` returns.

## Project Structure

See [architecture.md](architecture.md) for the full layout and module
boundaries. In brief:

- `src/disktide/` — all source code
- `tests/` — flat test suite
- `assets/` — TCSS stylesheets
- `tool/` — dev utilities (`gen_activity`, `bench_scan`, `diag_scan`,
  and the scan-benchmark harness: `make_homelike`, `tui_time`, `spy_agg`)
- `docs/` — documentation

## Key Conventions

### Code Style

- Python ≥ 3.10 features: `X | Y` unions, `slots=True`/`kw_only=True`
  dataclasses, `zip(strict=True)`. No `match` statements.
- Import `tomllib` and `StrEnum` from `disktide._compat` (backport on 3.10).
- Type annotations on all public APIs. `from __future__ import annotations`
  at the top of every module.
- Dataclasses with `slots=True` for models.

### Module Boundaries

The layering in [architecture.md](architecture.md#project-layout) is
enforced by convention:

- `scanner/`, `storage/`, `cleanup/`, `viz/` have no Textual imports and are
  testable without a running app.
- `services/` has no Textual dependency; `screens/` and `widgets/` can
  import everything above.
- CLI and TUI must never call `delete_targets()` or permanent filesystem
  primitives directly — only through `CleanupService`.
- Declarative policy loaders must never execute user-authored content.

### Startup Import Boundary

`app.py` must not import mode screens or services at module scope:

- Screens are imported inside `_launch_explorer()`.
- Services are lazy properties that import their module on first access.
- Teardown reads backing fields (`self.__scan_service`) so quitting from the
  welcome screen doesn't build a service to discard it.

`tests/test_startup_imports.py` enforces this with a budget on the resident
`disktide` module count.

### Scan Service and Consumers

- Product code submits `ScanRequest` to `ScanService` — never constructs
  `ScanEngine` directly.
- Textual consumers may only call `app.call_from_thread()` from the
  dispatcher callback.
- Consume `ScanProgressSnapshot`, `NodeAggregateUpdated`, and the terminal
  `ScanRun` result — don't compute a second set of totals.
- Consumer exceptions are isolated; add a regression test for new consumers.
- Directory workers scan direct entries only; descendants return through
  `TreeScanScheduler`, never by recursing in a worker.
- Keep `ScanEngine().scan(path)` compatibility for `tool/` scripts.

### Filesystem Event Backends

- Backends implement `collectors.events.base.EventBackend` and emit
  normalized hints only. No presentation or persistence imports.
- Optional packages must be probed and imported lazily.
- Events are never authoritative totals. Only full reconciliation writes
  snapshots.
- Overflow, watch limits, root loss, and restart must persist a degraded
  state.

## The Scanner Extension

`src/disktide/scanner/_scanfast.c` reads a whole directory and lstats its
entries in one GIL release. It is optional: `disktide/scanner/accel.py`
falls back to `_scanfast_py.py` when it is not importable, and the two
produce identical trees.

### Scanner extension in a development checkout

Editable installs build it. `pip install -e .` and `uv sync` run the same
`hatch_build.py` hook a wheel build does, and for an editable version it
compiles the object **in place**, into `src/disktide/scanner/`.

In place, and not into a temporary directory, for a reason worth knowing:
an editable install is a `.pth` file naming `<root>/src`, so `import
disktide` resolves to `src/disktide/` — a real package, which beats the
`disktide/` directory the wheel's copy leaves in `site-packages` (no
`__init__.py`, so only a namespace *portion*, and a regular package wins).
An editable install that force-included the object from a build directory
therefore put a perfectly healthy `.so` where nothing would ever look, and
`disktide doctor` said `python fallback` — which is what the maintainer's
own `uv sync` had been doing for the life of the extension.

Nothing reruns the hook when you *edit the C file*, though, so:

```bash
python tool/build_scanfast.py       # rebuild in place, then verify it loads
disktide doctor | grep Scanner      # says which reader is live
#   Scanner: native (_scanfast)
#   Scanner: python fallback (...)
```

The tool makes the same three decisions the hook makes — the compiler
search order, the macOS bundle flags, and loading the object in a subprocess
before believing it — and exits non-zero on any of them. `--print-command`
shows the invocation without running it; `--output DIR` builds somewhere
else.

`.so` is gitignored. Delete it to go back to the fallback, or set
`DISKTIDE_ACCEL=0` for one command. The suite has to pass both ways:

```bash
uv run pytest -q -n 4
DISKTIDE_ACCEL=0 uv run pytest -q -n 4
```

`tests/test_scan_accel.py` parametrises over both readers and skips the
native half where it was not built. `tool/dump_tree.py --backend
native|python` is the whole-tree version of the same check:

```bash
uv run python tool/dump_tree.py /some/tree --backend native -o a.tsv
uv run python tool/dump_tree.py /some/tree --backend python -o b.tsv
diff <(tail -n +2 a.tsv) <(tail -n +2 b.tsv)   # line 1 names the backend
```

### The two packaging traps

`hatch_build.py` compiles the extension for wheel builds and *skips
silently* when it cannot, so an sdist installs everywhere. Two things it has
to keep getting right:

1. **Compile outside the source tree — for a *wheel*.** A `.so` left under
   `src/disktide/scanner/` — which is exactly what the in-place build above
   leaves — is an ordinary package file to the next build, and one was swept
   into a *pure* wheel that then claimed `py3-none-any` while carrying an
   x86_64 binary. For the wheel version the hook compiles into a temporary
   directory and `force_include`s the result; `exclude = ["*.so", "*.pyd",
   "*.dylib", "*.c"]` on the wheel target is the belt to that brace, and it
   is what makes the editable version's in-place object safe to leave lying
   around. The editable version also fills
   `build_data["force_include_editable"]`, which is what hatchling reads
   instead of `force_include` when it builds an editable wheel, so the
   object is in `RECORD` and `pip uninstall` takes it away again.
2. **Find a compiler that exists.** `sysconfig`'s `CC` is whatever built the
   interpreter, and a uv-managed CPython says `clang`, which most Linux
   runners do not have. The hook tries `$CC`, then sysconfig's, then `cc`,
   `gcc`, `clang`, and takes the first one on `PATH`. `CC=/bin/false uv
   build --wheel` is the quickest way to exercise the pure path; so is
   `DISKTIDE_NO_EXTENSION=1`.

On macOS a Python extension is a bundle, not a shared library
(`-bundle -undefined dynamic_lookup`); the hook branches on that, and honours
`ARCHFLAGS` so a cibuildwheel cross-compile cannot silently produce a
host-architecture object.

## Distribution Checks

```bash
uv build
uv run python tool/verify_distribution.py
```

Two wheel shapes are valid and `verify_distribution.py` accepts both: a
`cp3xx` wheel carrying `disktide/scanner/_scanfast*.so`, or a `py3-none-any`
wheel carrying no compiled code. Anything *else* native in there is a
dependency that has stopped being pure, which is what the check is for.

CI installs the wheel into a clean environment and enforces ≤ 20 runtime
distributions, ≤ 20 MiB, and no third-party native extension — disktide's own
accelerator is exempt, because nothing requires it. CI also installs the
sdist twice, once with a compiler and once with `CC=/bin/false`, and asserts
the backend each one reports (`tool/check_doctor_backend.py`). A second
environment installs `disktide[watch]` and verifies event backend discovery.
See [release-process.md](release-process.md) for the tag and PyPI flow.

## How-To Guides

### Adding a Config Field

1. Add the field to the appropriate dataclass (`ScanConfig`, `MonitorConfig`,
   `UIConfig`, or `HostPaths`).
2. Add serialization in `save_config()` — skip `None` for optional fields.
3. Add deserialization in `load_config()` with a sensible default.
4. Add a roundtrip test in `tests/test_config.py`.

### Adding a Database Migration

1. Increment `CURRENT_VERSION`.
2. Add DDL statements under the new key in `MIGRATIONS`.
3. Put data backfill in `MIGRATION_CALLBACKS` — runs in the same transaction.
4. Keep pre-migration backup and rollback intact.
5. Add success, legacy-preservation, and interrupted-migration tests.

Never commit a schema version before DDL/backfill succeeds, fill unknown
legacy policy with current defaults, or delete a corrupt database as repair.

### Adding a Cleanup Rule

Add a rule to an existing schema-v1 TOML under `cleanup/rulepacks/`, or
create a new pack:

```toml
schema_version = 1
name = "my-pack"
version = "1.0.0"
description = "Project-local rebuildable artifacts"
default_enabled = true

[[rules]]
name = "my_rule"
description = "What this detects"
patterns = ["pattern1", "pattern2"]
parent_indicators = ["marker"]
path_context = ["*project*"]
min_age_days = 7
risk = "safe"
category = "my_category"
rebuild_hint = "Run the project rebuild command"
confidence = 0.9
default_action = "safe"
```

- No command, hook, or executor fields in the schema.
- Use `default_action = "detection-only"` when generic execution is wrong.
- Add positive, negative, indicator, age-boundary, invalid schema, scoring,
  and detection-only tests.
- Preserve old CleanupPlan payload readers when adding plan metadata.

### Adding a Screen

1. Create `screens/my_screen.py` subclassing `textual.screen.Screen`.
2. Define `BINDINGS` and `compose()`.
3. Install in `app.py` `_launch_explorer()` (not at module scope):
   ```python
   from disktide.screens.my_screen import MyScreen
   self.install_screen(MyScreen(...), name="myscreen")
   ```
4. Add a key binding in `DiskTideApp.BINDINGS` and a case in
   `action_switch_mode()`.
5. Add styles in the screen's own `DEFAULT_CSS`. Nothing loads
   `assets/default.tcss` — there is no `CSS_PATH` in `src/`. Rules that have
   to outrank a Textual widget's `DEFAULT_CSS` (including its `!important`
   ones) go in `DiskTideApp.CSS` instead.

### Adding a Visualization

1. Create layout/rendering in `viz/my_viz.py` — produce Rich Segments or use
   the braille canvas.
2. Reuse `VisualState`/`VisualDelta` — don't classify growth independently.
3. Create a Textual widget in `widgets/my_viz_view.py` that consumes a
   prebuilt model (not a repository).
4. Add a `TabPane` and screen-local key binding.
5. Test: current, diff, partial/incompatible, safe/no-color, narrow, resize,
   and bounded-large-tree cases.

## Testing Tips

- **Scanner**: use `tmp_path` fixtures with real directory trees.
- **Scan service**: cover all four terminals; assert no events after terminal;
  replay reconstructs the same view model.
- **Database**: use `:memory:` or `tmp_path`. The `db` fixture in
  `tests/test_storage.py` provides a migrated database.
- **Async**: `@pytest.mark.asyncio` for coroutines; `asyncio.run(go())` +
  `app.run_test()` for Textual flows.
- **Don't read the host**: inject values (`resolve_live_scan_render(...,
  cpu_count=2)`), pin config, or use `threading.Event` instead of sleep.
- **Visualization**: test layout separately from rendering. Cover state
  classification, coordinates, gap handling, selected-path identity,
  80×24/safe/no-color fallback, and 100k-node performance.

## Dev Utilities

| Script | Purpose |
|--------|---------|
| `tool/gen_activity` | Generate filesystem activity for testing watch/monitor. `--max-files`, `--max-size` caps. |
| `tool/bench_scan` | Scan timing. `--mode raw` (compatibility baseline), `events` (service delivery), `live` (view-model delivery). `--workers N`. |
| `tool/diag_scan` | Diagnostic scan: 1s heartbeat, 5s stall detector, per-directory hotspot table. `--profile` for `cProfile`. Designed for NFS/remote slowness where the TUI progress bar pulses but you can't see what's slow. |
| `tool/make_homelike` | Build the benchmark fixture: 88,000 dirs / 888,100 files, deterministic for a seed. Plans the exact entry count before it creates anything. |
| `tool/scratchguard` | The gate every generator above asks before writing in bulk: refuses `$HOME`, network filesystems, and paths without inode headroom. `python tool/scratchguard.py PATH --entries N` answers the same question from a shell. |
| `tool/tui_time` | Time one scan in the *real* TUI, under a private tmux server, with per-thread CPU. |
| `tool/spy_agg` | Aggregate a `py-spy record --format raw --threads` profile per thread. |
| `tool/soak_memory` | Scan one tree N times in one process and watch RSS. The memory half of `soak_scan`, which is a randomised *invariants* soak. |
| `tool/dump_tree` | Every node of one scan as sorted text, for byte-identity diffs. `--backend native\|python` picks the directory reader; the first header line names it. |
| `tool/check_doctor_backend` | Assert which scanner backend a `doctor --json` report names. Used by CI on each install shape. |
| `tool/build_scanfast` | Compile the optional scanner extension in place after editing `_scanfast.c`, then load it to check it imports. `--print-command`, `--dry-run`, `--output DIR`. |
| `tool/capture_glyphs` | Walk every screen in a private tmux server and report any non-ASCII glyph outside `disktide.glyphs`'s reviewed set. `--size 307x71 --size 120x32` by default; `--slow-tree N` makes the scan last long enough to photograph the progress overlay. |

### Benchmarking a scan

`bench_scan` does not paint, and the difference is not a rounding error: a
home directory it walked in 37 s took ~420 s in the explorer before the live
UI was paced. Every thread in the process shares one GIL, so a second of
drawing is a second the walk does not run. Measure headless first because it
is cheap and repeatable; confirm in `tui_time` because that is the program.

Build the fixture once:

```bash
python tool/make_homelike.py /local/scratch/homelike     # 88,000 dirs, 11.5 s on xfs
python tool/make_homelike.py /local/scratch/small --dirs 5000 --seed 7
python tool/make_homelike.py --dirs 5000 --seed 7        # the guarded default
```

Where it may write is decided by `tool/scratchguard.py`, which every generator
in `tool/` goes through, and which you can ask directly:

```bash
python tool/scratchguard.py /local/scratch/homelike --entries 976100
```

Three refusals, each naming the path, the reason, and what lifts it:

- **`$HOME`** -- lifted by `--allow-home`. A network home is usually quota'd
  by inode as well as by size, and this writes close to a million entries.
- **network filesystems** -- lifted by `--allow-network`, and by
  `$DISKTIDE_SCRATCH`. The mount is found by longest-prefix match against
  `/proc/mounts`, which matters: on an HPC login node `/users` is `autofs`
  and the `nfs4` that counts is mounted under it, so a first-match walk
  answers with the wrong filesystem. A path under `$DISKTIDE_SCRATCH` needs
  no flag at all: the designated place for a large fixture on this cluster is
  `/fs/scratch/...`, which is gpfs with no inode quota -- a parallel
  filesystem, so it matches the fstype list, and refusing the one directory
  the site provides for the job only taught people to type `--allow-network`
  on every call. The trust is network-only: a `$DISKTIDE_SCRATCH` under `~`
  is still refused without `--allow-home`, and headroom is still checked.
- **inode headroom** -- **no flag lifts this one.** `os.statvfs` alone is not
  enough: on the quota'd NFS home this was written for it reports orders of magnitude more
  free inodes while `quota` reports far fewer left, so `quota -w -u -p` is asked
  as well. A host without the `quota` binary (every CI runner) simply gets the
  `statvfs` answer; a missing tool never fails closed.

With no target the tree goes to `$DISKTIDE_SCRATCH` (or `$TMPDIR`)
`/disktide-<user>/<label>`, through the same three checks -- a `TMPDIR` under
`~`, which is common on HPC accounts, is refused exactly like a path typed out
by hand. Point `DISKTIDE_SCRATCH` at local disk -- or at your site's
scratch -- once, and every generator follows.

The test suite applies the network half of the same check to its own temp
root: `tmp_path` follows `TMPDIR`, so a network `TMPDIR` would put every tree
the suite builds on the quota'd filesystem. It exits 4 before collection with
the fix in the message (`export TMPDIR=/tmp`), or runs anyway with
`DISKTIDE_ALLOW_NETWORK_TMP=1`. It asks the guard the same question the
generators do, so a `TMPDIR` under `$DISKTIDE_SCRATCH` is accepted for the
same reason a fixture there is.

Then A/B against a *frozen* copy of the revision you are comparing to, so
neither side moves under you:

```bash
git worktree add /tmp/base <base-rev>
cp -r /tmp/base/src /tmp/frozen-src && cp -r /tmp/base/tool /tmp/frozen-tool

for rep in 1 2 3; do
  PYTHONPATH=/tmp/frozen-src /usr/bin/time -f 'base %e %U %S %M' \
      python /tmp/frozen-tool/bench_scan.py $FIX --workers 1 --mode live
  PYTHONPATH=$PWD/src       /usr/bin/time -f 'mine %e %U %S %M' \
      python tool/bench_scan.py       $FIX --workers 1 --mode live
done
```

The rules that make those numbers mean anything:

- **Frozen copy on `PYTHONPATH`, both sides.** An editable install points at a
  checkout that changes when you switch branches.
- **Interleave A and B**, one rep each, rather than running three of one and
  then three of the other. Host load on a shared login node drifts over
  minutes; interleaving turns that into noise instead of a result.
- **Three reps, report the median.** Treat anything inside ±10 % as noise.
- **`sleep 120` between runs on NFS.** The attribute cache holds for
  `acdirmax`, 60 s by default, so back-to-back runs measure a warmer server
  than the first one did.
- **Report `%U`/`%S`, not only `%e`.** Under a GIL, wall clock is roughly the
  sum of the threads' CPU, and CPU is far less sensitive to host load.
- Watch RSS too (`%M`): live mode holds a published frame and the
  copy-on-write clones behind it.

For the real thing, `tui_time` starts `python -m disktide` under its own tmux
server (`tmux -L`, never yours) with XDG_{CONFIG,DATA,CACHE,STATE}_HOME
redirected into a scratch directory -- the explorer saves config on some key
presses and a benchmark must not move yours:

```bash
python tool/tui_time.py $FIX --workers 1 --live off --size 307x69 \
    --label base --src /tmp/frozen-src --xdg /tmp/xdg
```

Read `threads_walking`, not `threads`: it is per-thread CPU *spent on the
walk*, with the interpreter's boot and the welcome screen's first render
subtracted off the front and the completion render off the back. CPython 3.12
does not name OS threads, so every key is `python#<tid>`; sort by CPU and they
read as walk workers, then the scheduler (which runs inside Textual's
`asyncio_0` worker thread), then `main`, the UI thread.

`--pyspy FILE` records a profile over the scan; aggregate it per thread with
`tool/spy_agg.py --grep <function>`. Sample counts only compare at the same
`--rate`.

Read the *scheduler* thread as its own number. It is one thread applying
every directory result, so its share of the process is a per-directory
price: on the 88,000-directory fixture at one worker, 100 Hz, it was 270 of
960 samples (28.1 % of the process, 31 µs a directory) and is now 152 of 822
(18.5 %, 17 µs). In `bench_scan` it is `MainThread`; in `tui_time` it is the
second-largest `python#<tid>` after the walk workers, because it runs inside
Textual's `asyncio_0` worker thread.

**A profile cannot see the garbage collector.** A collection runs inside
whichever allocation crossed the threshold, so py-spy charges its time to the
scanner's own frames — two rounds of profiling attributed a live scan's floor
to the publish path before `gc.callbacks` showed that a quarter of the scan
was collection. `bench_scan --json` reports a `gc` object
(`full_collections`, `young_collections`, `seconds`, `paused`) for exactly
this reason; read it before believing a profile about where a scan's time
went.

Memory is its own measurement and needs its own tool, because
`scanner/gcpause.py` pauses the collector for a walk and freezes the finished
tree:

```bash
python tool/soak_memory.py $FIX/d1 --iterations 20 --mode raw   # and --mode live
python tool/tui_time.py $FIX --rescans 3 --src $PWD/src --xdg /tmp/xdg ...
```

Two traps in reading those numbers. `gc.get_objects()` does **not** report the
permanent generation, so after a freeze it reads as an almost-empty heap and a
leak check built on it always passes. And repeated scans in one process grow
RSS by about 2 MB an iteration through allocator fragmentation, on this branch
and on every revision before it — so a soak's verdict is only meaningful
against the same soak on the base, never against zero.

## Running the TUI in Dev Mode

```bash
disktide                                   # normal launch
textual run --dev -c disktide              # live CSS reloading, DOM inspector
```
