# Contributing

Guide for developers working on disktide.

## Setup

```bash
git clone <repo-url> disktide && cd disktide
uv sync --locked
```

This creates an editable `.venv` from the committed `uv.lock`, including the
default development dependency group (pytest, pytest-asyncio, textual-dev).

## Running Tests

```bash
# full locked suite
uv run pytest tests/ -v

# specific module
uv run pytest tests/test_scanner.py -v

# with coverage (if pytest-cov installed)
uv run pytest tests/ --cov=disktide
```

The test suite covers the scanner and tree model, configuration,
snapshot/database storage, cleanup rule-pack validation/scoring/plans/actions,
visualizations, TUI navigation and modals, FS Overview/block
devices/benchmarking, progress reporting, system detection, welcome flow, and
migrations. Wave-specific cleanup contracts live in
`tests/test_cleanup_wave08.py`, `tests/test_cleanup_wave09.py`, and
`tests/test_cleanup_wave14.py`.
Filesystem-event normalization, dirty coalescing, overflow recovery, optional
dependency fallback, and MonitorService integration live in
`tests/test_watch_wave10.py`. Adaptive worker selection, giant-directory chunk
checkpoints, resource queue cancellation, and Monitor queue projection live in
`tests/test_scan_wave13.py`.

`PathSuggester` coroutine tests use `@pytest.mark.asyncio`. Most Textual app tests instead wrap an async helper with `asyncio.run(...)` and use `app.run_test()`; follow the style of the nearby tests.

## Project Structure

See [architecture.md](architecture.md) for the full layout. In brief:

- `src/disktide/` -- all source code
- `tests/` -- test suite (no subdirectories, flat layout)
- `assets/` -- TCSS stylesheets
- `src/disktide/collectors/platform/` -- isolated OS/procfs/sysfs/command probes
- `src/disktide/extensions/` -- typed capability contracts and strict declarative policy loaders
- `src/disktide/repositories/` -- persistence-neutral protocols and adapters
- `src/disktide/services/` -- CLI/TUI-neutral application services such as doctor and scan orchestration
- `tool/` -- development utilities (e.g., `gen_activity` for generating test data)
- `docs/` -- documentation

## Key Conventions

### Code Style

- Python >= 3.11 features are used freely: `X | Y` union syntax, `tomllib`, `slots=True` dataclasses.
- Type annotations on all public APIs. `from __future__ import annotations` at the top of every module.
- Dataclasses with `slots=True` for models (`FSNode`, `Snapshot`, `SizeDelta`).

### Module Boundaries

- **models/** -- legacy-compatible pure data structures; new snapshot contracts live in `domain/`.
- **collectors/platform/** -- all platform-specific probes; callers consume structured results rather than `/proc`, `/sys`, or commands directly.
- **domain/** -- framework-independent metric, policy, scan, snapshot, compatibility, delta, and visualization contracts.
- **services/** -- application orchestration with no Textual dependency.
- **repositories/** -- persistence-neutral protocols plus concrete adapters; product entry points use factories/protocols rather than importing SQLite.
- **collectors/local_scanner.py** -- the only product adapter that constructs the compatibility `ScanEngine`.
- **scanner/** -- filesystem I/O only. No Textual imports.
- **storage/** -- SQLite I/O only. No Textual imports.
- **cleanup/** -- detectors, explainable scoring, packaged TOML rule packs, and low-level executors. No Textual imports and no product policy decisions.
- **services/cleanup.py** -- the only product cleanup policy/execution entry point; owns plan, revalidation, audit, and undo.
- **monitor/** -- alerting, tree diffs, scan scheduling. No Textual imports.
- **viz/** -- rendering logic. Produces Rich Segments, no direct Textual widget deps.
- **presentation/tui/viewmodels/** -- shared TUI vocabulary and formatting; no repository access.
- **screens/** and **widgets/** -- Textual UI layer. Can import everything above.

This layering means the scanner, storage, cleanup, and viz modules are testable without a running Textual app. CLI and TUI must never call `delete_targets()` or permanent filesystem primitives directly.

Declarative policy loaders must never execute user-authored content. Treat rule
scores as ordering metadata; every filesystem action still passes through
`CleanupService` identity/boundary revalidation and audit.

### Scan Service and Consumers

- Product code submits `ScanRequest` to `ScanService`; screens and CLI commands do not construct `ScanEngine`.
- Every event consumer must tolerate delivery from the service dispatcher thread. Textual consumers may only call `app.call_from_thread()` from that callback.
- Do not compute a second set of totals in a consumer. Consume `ScanProgressSnapshot`, `NodeAggregateUpdated`, and the terminal `ScanRun` result.
- Consumer exceptions are isolated by the service. Add a regression test whenever a new consumer is introduced.
- Use `ScanEventRecorder` plus `ProgressViewModel`/`TreeViewModel` for replay tests. Journals must have one run id, contiguous sequences, and one final terminal event.
- Keep high-frequency event payloads coalescible. Do not bypass the bounded run mailbox with direct presentation callbacks.
- Directory workers scan direct entries only; descendants must return through `TreeScanScheduler` rather than recursively occupying a worker.
- Keep one owner per `scandir` cursor. Direct entries cross the worker/coordinator boundary only through bounded chunks.
- Automatic worker selection must remain explainable and path-aware; explicit `workers` is an exact override.
- All product scans must acquire `ScanResourcePolicy` slots so queue state and cancellation remain observable.
- Keep `ScanEngine().scan(path)` compatibility for `tool/bench_scan.py` and `tool/diag_scan.py` until the compatibility facade is intentionally retired.

### Filesystem Event Backends

- Backends implement `collectors.events.base.EventBackend` and emit normalized
  hints only. They do not import presentation or persistence adapters.
- Optional packages must be probed and imported lazily. Core periodic scans,
  doctor, CLI, and TUI must work when no backend package is installed.
- Never treat events as authoritative totals or an audit stream. Local dirty
  scans use `ScanService`; only successful full reconciliation writes snapshots.
- Overflow, watch limits, root loss, restart, and lease expiry must persist a
  degraded/full-reconciliation-required state.
- Add backend-neutral tests with a fake backend. Platform integration tests may
  require the `[watch]` extra but cannot weaken the core clean-install gate.

## Distribution Checks

```bash
uv build
uv run python tool/verify_distribution.py
uv run python tool/benchmark_wave13.py --output /tmp/wave-13-benchmark.json
uv run python tool/benchmark_wave15.py --output /tmp/wave-15-benchmark.json
```

The CI minimal-install job installs the wheel into a fresh environment, runs
`disktide doctor`, a small scan, and periodic watch smoke, and enforces at most
20 runtime distributions, at most 20 MiB of installed files, and no native
extension. A second clean environment installs `disktide[watch]`, verifies
backend discovery, and runs strict `watch --events` smoke.
See [release-process.md](release-process.md) for the tag and PyPI flow.

### Configuration

`config.py` uses plain dataclasses (not Pydantic). TOML serialization is manual (line-by-line string building in `save_config`). When adding a new config field:

1. Add the field to the appropriate dataclass (`ScanConfig`, `MonitorConfig`, `UIConfig`, or `HostPaths`)
2. Add serialization in `save_config()` -- skip `None` values for optional fields
3. Add deserialization in `load_config()` with a sensible default
4. Add a roundtrip test in `tests/test_config.py`

### Database Migrations

`storage/migrations.py` uses a `schema_version` table. Snapshot format and API
versions are separate domain metadata and must not be inferred from that table.
To add a migration:

1. Increment `CURRENT_VERSION`
2. Add the ordered SQL statements under the new integer key in `MIGRATIONS`
3. Put data backfill in a `MIGRATION_CALLBACKS` entry; it runs inside the same transaction
4. Keep the pre-migration backup and rollback behavior intact
5. Add success, legacy-data-preservation, and interrupted-migration tests

Do not commit a schema version before its DDL/backfill succeeds, silently fill
unknown legacy policy with current defaults, or delete a corrupt database as a
repair strategy.

## Adding a New Cleanup Rule

1. Add the rule to the appropriate schema-v1 TOML file under
   `cleanup/rulepacks/`, or add a new pack with unique lowercase identifiers:

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

2. Do not add command, import, hook, script, or arbitrary executor fields. Use
   `default_action = "detection-only"` when generic filesystem execution is not
   the correct provider boundary.
3. Add positive, negative, parent-indicator/path-context, age-boundary, invalid
   schema, scoring, and detection-only cases in `tests/test_cleanup_wave09.py`,
   plus Wave08 revalidation coverage when the safety contract changes.
4. Preserve old CleanupPlan payload readers when adding plan metadata. The
   schema-v6 cleanup base tables, current database schema v10, and CleanupPlan
   payload v2 are independent version numbers. Runtime action transitions must
   use `update_cleanup_action()` rather than rewriting the full plan.

## Adding a New Screen

1. Create `screens/my_screen.py` subclassing `textual.screen.Screen`.
2. Define `BINDINGS` and `compose()`.
3. Install the screen in `app.py` `_launch_explorer()`:
   ```python
   self.install_screen(MyScreen(...), name="myscreen")
   ```
4. Add a key binding in `DiskTideApp.BINDINGS` and a case in `action_switch_mode()`.
5. Add styles in `assets/default.tcss`.

## Adding a New Visualization

1. Create the layout/rendering logic in `viz/my_viz.py`. It should produce Rich `Segment` objects or use the braille canvas.
2. Reuse `VisualState`/`VisualDelta` and the shared presentation vocabulary for snapshot-time semantics; do not classify growth independently in the widget.
3. Create a Textual widget in `widgets/my_viz_view.py` that calls the renderer and consumes a prebuilt model rather than a repository.
4. Add a `TabPane` in the relevant screen and a screen-local key binding.
5. Add current, diff, partial/incompatible, safe/no-color, narrow, resize, and bounded-large-tree tests.

## Testing Tips

- **Scanner tests**: Use `tmp_path` fixtures to create real directory trees. The scanner operates on real filesystems, not mocks.
- **Scan service tests**: Cover complete, partial, cancelled, and failed terminals; assert no events follow a terminal event and replay reconstructs the same view model.
- **Database tests**: Use in-memory SQLite (`:memory:`) or `tmp_path` for the db file. The `db` fixture in `tests/test_storage.py` provides a connected, migrated database.
- **Async tests**: Use `@pytest.mark.asyncio` for focused coroutine tests. For Textual app flows, use the established `asyncio.run(go())` + `app.run_test()` pattern.
- **Visualization tests**: Test layout computation separately from rendering. Verify shared state classification, rectangle coordinates, arc angles, gap handling, selected-path identity, 80x24/safe/no-color fallback, and the 100k-node performance bound.

## Dev Utilities

The `tool/` directory contains helper scripts:

- `gen_activity` -- generates filesystem activity (creates/modifies/deletes files) for testing the watch/monitor features. Supports `--max-files` and `--max-size` caps.
- `bench_scan` -- one-shot scan timing. Default `--mode raw` prints the stable `wall-time / dirs / files / size / rate` compatibility baseline through `ScanEngine().scan()`. `--mode events` measures service delivery, and `--mode live` additionally measures bounded view-model delivery without starting Textual. All modes accept `--workers N`.
- `diag_scan` -- diagnostic scan with a 1-second heartbeat (current path + dirs/files/GB), a stall detector (`STALL <sec>` when no counter has moved for 5 s), and a per-directory hotspot table at the end ranked by wall-clock time. `--profile` wraps the scan in `cProfile` and dumps the top callees by cumulative time at the end. `--workers N` pins thread count. Designed for diagnosing remote/NFS slowness where the TUI's progress bar pulses but you can't see *what* is slow. The scripts degrade gracefully across internal API changes; see `tests/test_tools.py` for the contract they rely on.

## Running the TUI in Dev Mode

```bash
# normal launch
disktide

# with Textual dev tools (live CSS reloading, DOM inspector)
textual run --dev -c disktide
```
