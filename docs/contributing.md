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
| `readdir` | `os.scandir` returning filesystem order, not creation order | Chunked sort window falling back to filesystem order at `entry_chunk_size=1` |
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

## Distribution Checks

```bash
uv build
uv run python tool/verify_distribution.py
```

CI installs the wheel into a clean environment and enforces ≤ 20 runtime
distributions, ≤ 20 MiB, no native extension. A second environment installs
`disktide[watch]` and verifies event backend discovery.
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
5. Add styles in `assets/default.tcss`.

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
| `tool/make_homelike` | Build the benchmark fixture: 88,000 dirs / 888,100 files, deterministic for a seed. |
| `tool/tui_time` | Time one scan in the *real* TUI, under a private tmux server, with per-thread CPU. |
| `tool/spy_agg` | Aggregate a `py-spy record --format raw --threads` profile per thread. |
| `tool/soak_memory` | Scan one tree N times in one process and watch RSS. The memory half of `soak_scan`, which is a randomised *invariants* soak. |

### Benchmarking a scan

`bench_scan` does not paint, and the difference is not a rounding error: a
home directory it walked in 37 s took ~420 s in the explorer before the live
UI was paced. Every thread in the process shares one GIL, so a second of
drawing is a second the walk does not run. Measure headless first because it
is cheap and repeatable; confirm in `tui_time` because that is the program.

Build the fixture once:

```bash
python tool/make_homelike.py /local/scratch/homelike     # 88,000 dirs, 11.5 s on xfs
```

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
