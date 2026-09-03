# Architecture

Technical overview of disktide's internals for anyone reading or extending
the codebase.

Architecture decisions are recorded as ADRs in the companion
`artifacts-disktide` repository under `adr/`.

## Project Layout

```
src/disktide/
  __main__.py            CLI entry point (Click)
  app.py                 Textual App, screen management
  config.py              TOML config load/save, dataclasses
  glyphs.py              Unicode/ASCII glyph selection
  metrics.py             Size vs. file-count view metric helpers
  rendering.py           Process-wide safe-rendering state and render epoch

  domain/
    alerts.py            Alert rule/event contracts
    cleanup.py           Persistent plan/action/identity/audit contracts
    delta.py             Compatibility decisions + shared delta result
    metrics.py           MetricId + StorageMeasurements semantics
    monitor.py           Definitions, status, history, retention contracts
    policy.py            Explicit ScanPolicy metadata
    provisional.py       Bounded canonical/provisional current-state contracts
    scan.py              ScanRequest/ScanRun/status/event contracts
    snapshot.py          Snapshot format v2 metadata
    visualization.py     Shared delta/trend/heatmap visual contracts

  collectors/
    local_scanner.py     ScanEngine adapter used by ScanService
  collectors/events/
    base.py              Backend-neutral filesystem event protocol
    native.py            Optional Linux inotify adapter and lazy probe
  collectors/platform/
    base.py              Portable adapter + shared capability probes
    linux.py             procfs/sysfs/lsblk implementation
    portable.py          Conservative macOS/Windows/unknown adapters
    models.py            Mount, block-device, and ProbeResult data

  extensions/
    capabilities.py      CapabilityId/status/reason public vocabulary
    cleanup_rules.py     Strict TOML rule-pack schema and isolated loader

  services/
    alerts.py            Rule CRUD + snapshot/delta evaluation
    cleanup.py           Plan, overlap, revalidation, execution, audit, undo
    compare.py           Policy-aware snapshot selection + compare reports
    doctor.py            Human + JSON installation diagnostics
    monitor.py           Shared management and foreground host pipeline
    provisional.py       Subtree overlay merge and current-tree materialization
    retention.py         Rollup planning, pins, budget maintenance
    scan.py              Run lifecycle, cancellation, event dispatch
    scan_consumers.py    Progress/tree view models and event replay
    snapshots.py         Persist successful ScanRun results
    visualization.py     Bounded Compare/Monitor query + view-model cache
    watch.py             Bounded dirty-path debounce/coalescing

  repositories/
    alerts.py            AlertRepository protocol
    cleanup.py           CleanupRepository persistence contract
    monitors.py          MonitorRepository + RetentionRepository protocols
    snapshots.py         SnapshotRepository protocol + status contract
    sqlite.py            SQLite repository adapter

  scanner/
    benchmark.py         Opt-in mount throughput probe
    blockdev.py          Compatibility facade over platform adapter
    engine.py            ScanEngine compatibility facade
    scheduler.py         Bounded all-tree directory scheduler + COW frames
    walker.py            Recursive os.scandir() compatibility walker
    progress.py          Throttled progress reporting
    sysinfo.py           System detection for adaptive threading

  models/
    tree.py              FSNode -- the core filesystem tree
    snapshot.py          Compatibility imports
    patterns.py          CleanupRule, CleanupTarget, RiskLevel

  storage/
    database.py          SQLite backend (WAL mode)
    migrations.py        Schema versioning

  cleanup/
    detector.py          Walk tree and match against rules
    rules.py             Rule-catalog compatibility accessors
    scoring.py           Deterministic opportunity score/confidence
    rulepacks/           Packaged schema-v1 TOML policy
    actions.py           Trash, quarantine, restore, guarded delete primitives

  monitor/
    diff.py              In-memory comparison using shared SizeDelta

  viz/
    layout.py            Viewport-bounded top-N + aggregate remainder
    treemap.py           Squarified treemap layout + rendering
    sunburst.py          Ring chart, supersampled half-block rasterizer
    braille.py           ColorBrailleCanvas -- per-cell color voting
    cellgeom.py          Layered cell-aspect resolver + XTWINOPS probe
    colors.py            5 color schemes, HSL utilities

  screens/
    welcome.py           Welcome screen with path completion
    explorer.py          Tree + visualization (treemap/sunburst/details)
    cleanup.py           Plan review, safe apply, history, and undo workflow
    monitor.py           Monitor Center management + history/alerts/retention
    fs_overview.py       Mounted filesystems, block devices, benchmark
    settings.py          Configuration editing

  widgets/
    size_tree.py         Tree[FSNode] with size bars, lazy loading
    breadcrumb.py        Path breadcrumb navigation
    alert_editor.py      Alert rule create/edit modal
    info_panel.py        Details for selected node
    monitor_editor.py    Monitor definition create/edit modal
    treemap_view.py      Treemap Textual widget
    sunburst_view.py     Sunburst Textual widget
    trend_chart.py       Historical size line chart
    growth_heatmap.py    Path-by-time persistent-growth matrix
    scan_progress.py     Scan progress overlay
    cleanup_modal.py     Plan summary + separate permanent confirmation
    cleanup_map.py       Bounded synchronized Age/Size opportunity map
    cleanup_history.py   Estimated/isolated/purged/actual/undone history
    confirm_modal.py     Reusable y/n confirmation dialog
```

## Platform Capability Boundary

All procfs, sysfs, mount-table, `lsblk`, and storage-medium probes live
behind `PlatformAdapter`. Probe failures are values (`available`, `degraded`,
`unavailable`) with a reason — not exceptions. `scanner/sysinfo.py` and
`scanner/blockdev.py` are compatibility facades that delegate to the active
adapter.

`disktide doctor` consumes the same capability snapshot as FS Overview. Its
JSON output redacts application paths by default.

## Scan Service and Event Protocol

`ScanService` is the single entry point for CLI scans, Explorer scans,
cleanup discovery, and foreground monitor hosts. It validates a
`ScanRequest`, resolves the active platform adapter, creates a stable
`ScanRun` id, owns cancellation, and returns one terminal status:
`completed`, `partial`, `cancelled`, or `failed`.

The domain contract (`domain/scan.py`) has no Click, Textual, or SQLite
imports. Every event carries the same `run_id`, a contiguous `sequence`, and
a `phase`:

- `ScanQueued`, `ScanStarted`, `ScanPhaseChanged`
- `DirectoryQueued`, `ScanProgressUpdated`, `DirectoryCompleted`
- `NodeAggregateUpdated` — live/final tree handoff
- `AccessError` — explicit partial coverage
- Exactly one terminal: `ScanCompleted`, `ScanCancelled`, or `ScanFailed`

**Event replay**: `ScanEventRecorder` replays captured runs into
`ProgressViewModel` and `TreeViewModel`. Replay rejects mixed run ids,
sequence gaps, and events after a terminal event.

**Consumer isolation**: consumer callbacks are quarantined after their first
exception. The failure is recorded on `ScanRun.consumer_errors` without
aborting the scanner or other consumers.

**Dispatch**: events flow through a bounded mailbox (64 pending by default)
on one service-owned dispatcher thread. Progress, queued/completed-directory,
and non-final tree events coalesce; terminal events do not. Sequence numbers
are assigned at delivery so journals stay contiguous after coalescing.

See ADR 0002 (scan service), ADR 0003 (scheduler and backpressure), and
ADR 0012 (adaptive live scan engine).

## Scanner

### Threading Model

`collectors/local_scanner.py` adapts `ScanService` requests to
`ScanEngine`, a compatibility facade over `TreeScanScheduler`. Each
directory is one non-recursive task:

- One worker owns its `scandir` cursor and streams entries in bounded chunks
  (256 entries by default).
- Child-directory jobs materialize only as slots open — no queue holds the
  full tree.
- Each chunk installs file nodes and zero-valued directory placeholders.
- Aggregates update in O(1) deltas; children sort only when a directory
  settles.
- Live roots use generation-based copy-on-write: once published, later
  mutations clone only modified paths, so old frames remain safe to read.

The main Textual event loop stays on the main thread. Long-running scans
use `@work(thread=True)`. The screen consumes typed events and marshals
them to the UI thread via `app.call_from_thread()`.

The recursive `scanner.walker.scan_directory()` and `ScanEngine().scan(path)`
APIs remain for diagnostics and `tool/` scripts; product code does not
construct the engine directly.

### Adaptive Worker Count

`sysinfo.select_scan_workers()` resolves policy against the actual scan
path. Automatic selection considers CPU availability, load, memory,
filesystem/media classification, and a bounded 64-entry/75ms metadata
sample:

| Condition | Workers |
|-----------|---------|
| Low-latency local or RAM-backed storage | 1 |
| Rotational local storage | ≤ 2 |
| Network or measured high-latency storage | ≤ 4 |
| High host load | reduced |
| < 512 MB available memory | 1 |
| Probe error | 1 (conservative fallback) |

An explicit `workers` value skips all detection.

### Progress Reporting

`ProgressThrottle` batches callbacks to 100ms intervals.
`ScanProgressSnapshot` captures completed/queued directories, queue depth,
active workers, files, bytes, current path, and elapsed time. Progress
reports Logical bytes because Unique requires global hardlink reconciliation;
the final tree additionally carries Allocated and Unique.

### Measurement and Scope Policy

- **Logical**: `st_size` payload aggregate.
- **Allocated**: `st_blocks × 512` per visible file/symlink path.
- **Unique**: one deterministic lexical owner per `(st_dev, st_ino)`.
- Missing `st_blocks` → unavailable, never zero.
- One-filesystem mode stops at device boundaries, keeping an `xdev` node.
- Descendant pseudo mounts excluded by default; explicit pseudo root allowed.
- Max-depth and policy exclusions are separate from access errors.

See ADR 0001 for the storage metric contract.

### Error Resilience

Each `os.scandir()` entry is wrapped in try/except. A permission error on
one directory stores the error in `FSNode.error` and the scan continues with
partial results.

### Symlink Handling

Symlinks are never recursed into — stored as leaf nodes sized by the link
itself (`lstat`). Target classification (readlink + follow stat) is
**deferred**: `make_symlink_node` pays only the one `entry.stat`, and
`classify_symlink` runs on demand (Details panel, `i` navigation). The
result is cached via `link_classified`.

The engine eagerly classifies the first 100 symlinks at the scan root so
`disktide ~` shows `→ target` decorations immediately without regressing
scan-root directories that contain hundreds of thousands of symlinks.

## Data Model

### FSNode

The core tree structure (`models/tree.py`):

```python
@dataclass(slots=True)
class FSNode:
    name: str              # basename
    path: str              # absolute path
    size: int              # subtree total (files + children)
    own_size: int          # own bytes, or direct file/link bytes for a dir
    allocated_size: int | None
    unique_allocated_size: int | None
    file_count: int
    dir_count: int
    is_dir: bool
    mtime: float           # last modification epoch
    depth: int
    children: list[FSNode]
    error: str | None
```

Key behaviors: `sorted_children` (lazy-cached sort by size descending),
`walk()` / `walk_dirs()` (depth-first iteration), `find(path)` (recursive
lookup), `size_percent(parent_size)`.

The model also carries `StorageMeasurements`, device/inode/link identity,
hardlink ownership, partial-access aggregates, changed-during-scan counts
(`vanished`, `vanished_count`, `vanished_subtree_count`), lazy symlink
classification, filesystem-boundary/pseudo exclusion markers, max-depth
truncation, and the root `ScanPolicy`.

Two hot paths make the field order a contract: `scanner.walker.make_file_node`
constructs a node positionally for the first 28 fields, and
`FSNode.shallow_copy` (also bound as `__copy__`) does the same for all of
them. New fields go at the end; `tests/test_tree.py` pins both lists.

### Snapshot

`domain/snapshot.py` defines snapshot format v2 independently of SQLite.
Records the stable scan result plus metadata for deciding whether a
comparison is meaningful:

```python
@dataclass
class Snapshot:
    id: int | None
    root_path: str
    timestamp: datetime               # aware UTC
    total_size: int
    total_allocated_size: int | None
    total_unique_allocated_size: int | None
    file_count: int
    dir_count: int
    scan_duration: float
    format_version: int                # currently 2
    metric_semantics_version: str
    selected_metric: MetricId
    policy: ScanPolicy | None
    scanner_version: str | None
    scan_run_id: str | None
    completion_status: str
    partial: bool
    error_count: int
    root_device_id: int | None
    root_inode: int | None
    root_filesystem: str | None
    legacy: bool
    inference_source: str
```

Legacy rows remain readable. Missing policy fields are represented as
unknown — never filled with current defaults.

### SizeDelta

`domain/delta.py` is shared by CLI, Monitor, and visualizations:

```python
@dataclass
class SizeDelta:
    path: str
    old_size: int
    new_size: int
    is_new: bool
    is_removed: bool
    is_dir: bool
    old_allocated_size: int | None
    new_allocated_size: int | None
    old_unique_size: int | None
    new_unique_size: int | None
    old_file_count: int
    new_file_count: int
```

`CompatibilityResult` records `compatible`, `compatible-with-warning`, or
`incompatible` with field-level explanations. `CompareResult` combines the
decision with deterministic path deltas and total changes.

## Snapshot Repository and Compare

Product code depends on `SnapshotRepository`, not SQLite. The protocol
exposes snapshot save/list/load, measurement reconstruction, history,
deletion/pruning, status, and read-only clone. `SQLiteSnapshotRepository`
is the current adapter.

**Compare flow**: `CompareService` resolves selectors (`latest`, `previous`,
`oldest`, numeric ids, `--since` windows), then checks root identity, metric
semantics, xdev, pseudo-filesystem, max-depth, symlink, hardlink, exclude,
and partial coverage before loading measurements. Incompatible comparisons
are blocked unless the caller explicitly requests a raw/untrusted diff.

**Targeted queries**: `load_measurement_series()` resolves only requested
paths across an ordered snapshot window; `list_changed_paths()` streams
metric-ranked candidates; `load_visualization_projection()` builds paired
sparse trees with selected ancestors and aggregate remainder nodes.

`get_size_history(path)` combines baseline rows and deltas, then
forward-fills unchanged snapshots. Monitor history queries add revision, pin,
rollup, partial, missing, removed, and incompatible state so the TUI never
converts an absent subtree into a false zero.

## Monitor Service and Foreground Hosts

`MonitorService` is the command/query boundary shared by CLI and TUI.
Presentation code submits intents (create, update, pause, run, reconcile,
host, archive, pin, retention, alert); it does not write SQLite directly.

### Three State Dimensions

| Dimension | Values |
|-----------|--------|
| Desired | `enabled`, `paused`, `archived` |
| Activity | `no-host`, `waiting`, `queued`, `scanning`, `reconciling`, `stopping` |
| Health | `unknown`, `healthy`, `warning`, `failed`, `blocked` |

An enabled definition does not imply background execution.

### Host Lifecycle

The TUI, `disktide watch`, and an external systemd unit all acquire the same
expiring repository lease and heartbeat it. Start-to-start UTC due times are
persisted; process waits use a monotonic clock. One monitor never overlaps
itself. Repeated run-now requests coalesce.

Every hosted run uses one pipeline: scan → persist snapshot → evaluate
alerts → apply retention → record status → publish events.
`ScanResourcePolicy` provides FIFO scheduling; by default one run is active
per filesystem device, while unrelated devices proceed concurrently.

### Event Acceleration

Optional event acceleration inserts into the host pipeline instead of
creating a second scheduler. `collectors.events` normalizes
create/modify/delete/move, overflow, root-lost, and backend-error signals.
One backend and bounded `DirtyPathTracker` attach to each held lease.

- Ordinary dirty subtrees are rescanned without writing a formal snapshot.
- Scheduled, manual, startup/restart, and overflow recovery scans are full
  root scans — the only runs that persist canonical history.
- Events during a full scan stay dirty for the next reconciliation.

On Linux, watch setup uses a scan-driven handoff: the root descriptor is
installed before event capture, then each directory is registered just before
its `scandir` cursor opens. `auto` mode stops a failed backend and falls
back to periodic-only for the rest of that host session.

### Canonical vs. Provisional State

A successful local reconciliation can replace bounded subtrees in an
in-process `ProvisionalCurrentState`. Its compact summary is persisted for
status/diagnostics but **never** creates a snapshot or feeds history,
compare, alerts, exports, or retention.

Full reconciliation atomically advances the canonical baseline and clears the
overlay. Uncertain hardlink, partial, policy, device, excluded-mount,
overflow, backend, or restart states invalidate the overlay and require a
full scan.

### Retention

Keeps recent snapshots densely, promotes older history into hourly/daily/
weekly representatives, preserves pinned snapshots and the newest snapshot in
every revision. Deleting a required baseline first materializes the earliest
survivor. The soft budget triggers maintenance; the hard budget blocks
snapshot persistence (not the in-memory scan).

### Alerts

Persisted per monitor, evaluated from canonical snapshot measurements. Rules
cover absolute size/growth, percentage growth, free bytes, free inodes, and
new large items. Events record snapshot ids, observed/threshold values,
severity, confidence, cooldown suppression, and suppression reason. Partial
snapshots create suppressed audit events instead of presenting low-confidence
triggers as definitive.

See ADR 0005 (host, scheduling, retention, alerts) and ADR 0009 (events).

## Database

SQLite with WAL mode at `~/.local/share/disktide/data.db` (respects
`XDG_DATA_HOME`; legacy directory names retained for upgrade compatibility).

### Schema (v10)

Database schema v10 is distinct from snapshot format v2 and the public
snapshot API version.

| Table | Contents |
|-------|----------|
| `monitor_definitions` | Path, label, revision, desired state, interval, metric, policy, workers, retention |
| `monitor_status` / `monitor_leases` | Activity/health, next due, progress, event/backend/confidence diagnostics, lease/heartbeat |
| `monitored_roots` | Stable root path, device, inode, filesystem, last-seen |
| `scan_runs` | Run id, timestamps, status, duration, policy, coverage, monitor/revision, trigger |
| `snapshots` | Legacy-compatible totals, baseline/delta links |
| `snapshot_metadata` | Format/API versions, metric semantics, policy, identity, coverage, rollup kind |
| `paths` | Interned file/directory paths with parent, basename, depth |
| `nodes` | Full measurements for baseline snapshots |
| `deltas` | Changed/added/removed state for non-baseline snapshots |
| `snapshot_pins` / `snapshot_rollups` / `retention_runs` | Protection, provenance, maintenance audit |
| `alert_rules` / `alert_events` | Rule config and trigger/suppression audit |
| `cleanup_plans` / `cleanup_actions` / `cleanup_audit` | Plan provenance, ordered actions, validation/execution/undo/purge events |

Dynamic path/id lookups use bounded SQLite bind batches — placeholder count
never grows with tree or plan size.

### Baseline/Delta Storage

The first snapshot for a root is a baseline; another is stored every 50
snapshots. Intermediate snapshots persist only changed directory rows against
the previous state. When retention deletes a baseline, the earliest surviving
dependent is materialized and promoted.

### Snapshot Queries

`list_snapshots(root_path)` uses bidirectional matching by default (exact,
ancestor, and descendant roots). `strict_path=True` restricts to exact
matches.

`load_tree()`, `load_measurements()`, and `compare_snapshots()` are explicit
full-state APIs. Interactive use prefers targeted contracts:
`load_measurement_series()`, `list_changed_paths()`, and
`load_visualization_projection()`.

### Migration

Before changing a non-empty database, migration writes a SQLite backup
(`data.db.pre-v10.bak`). All DDL, backfill, and schema version changes run
in one transaction; failure rolls back without advancing `schema_version`.

If migration or writes fail but the database is readable, it opens read-only
so history remains available. If corrupt, the app uses an in-memory degraded
repository for scan-only use. It never deletes or overwrites the database as
automatic repair.

## Cleanup System

### Rule Matching

`extensions/cleanup_rules.py` validates schema-v1 TOML and builds one
catalog from packaged and user-owned packs. Pack loading fails independently
— malformed TOML, unknown fields, name collisions become doctor-visible
issues without hiding valid packs. The schema has no executor or code field.

`detect_targets(root)` walks the FSNode tree and matches each node:

1. Name matches a rule's glob patterns
2. (Optional) Parent-indicator files exist
3. (Optional) Path context glob matches
4. (Optional) mtime exceeds `min_age_days`

### Built-in Rule Packs

| Pack | Coverage | Default policy |
|------|----------|----------------|
| `python` | Bytecode/tool caches, virtualenvs, package builds | safe/preview |
| `node` | Dependencies, frontend builds, tool caches | safe |
| `rust` | Cargo `target` with `Cargo.toml` indicator | safe |
| `general` | Aged logs/temp, OS metadata | safe |
| `ide` | `.idea` / `.vscode` project metadata | preview |
| `containers` | Docker/BuildKit-style cache paths | detection-only |

Scoring is a documented 0–100 formula: 35% log-size, 25% age, 20% inverse
risk, 10% rebuildability, 10% confidence. Score never authorizes an action.

### Plan Creation

`CleanupService.create_plan()` captures `lstat` identity, rule/pack
provenance, versions, policy, risk, age, score, confidence, and logical
reclaim estimate. Ancestor/descendant overlap is resolved before persistence
— a parent action subsumes its children so bytes and execution count once.

Schema v9 stores plan metadata separately from ordered action rows. Runtime
status changes update one action at a time. Legacy payload-only plans remain
readable.

### Revalidation and Protected Paths

Before each action, `CleanupService` re-checks device/inode/mode/mtime/size,
re-measures directory contents, rechecks rule match, age, mount boundaries,
and protected paths (`/`, scan root, mount roots, database directory,
quarantine roots). Changed, missing, or replaced targets are skipped with an
audit reason.

### Safe Executors and Undo

Normal apply first attempts an atomic same-filesystem Freedesktop Trash
rename. Fallback: `QuarantineExecutor` creates a mode-0700 sibling
directory. Both paths:

- Open and verify the source parent directory
- Repeat target identity check immediately before the dir-fd rename
- Verify destination identity and roll back on mismatch
- Never substitute copy+delete

Both persist undo metadata. Undo refuses to overwrite a newly created
original path.

Quarantine uses a constant-size `.ledger.json` for capacity accounting
rather than rescanning manifests before every move.

### Permanent Deletion

Separate from safe apply, requires typed `DELETE <plan-id>` confirmation.
Supported POSIX systems stage a verified file under the same parent and
unlink by directory fd. Direct permanent directory deletion is blocked —
directories must enter quarantine first, then be explicitly purged. Platforms
without dir-fd primitives block permanent deletion entirely.

Every validation, intent, result, and undo is appended to `cleanup_audit`.
If the pre-action audit write fails, no filesystem action is attempted.

## Visualization

### Space-Time Contracts

`domain/visualization.py` classifies visual state: new, removed, growth,
shrink, unchanged, partial, incompatible, missing. `DiffFrame`, typed Trend
points/series, and Heatmap intervals retain path identity, metric,
confidence, and snapshot ids. Widgets never compare snapshots or query SQLite.

`VisualizationService` consumes `MonitorHistory` and `SnapshotRepository`.
It blocks incompatible pairs, caches sparse diff frames and bounded
path-series results, and builds Heatmap intervals from one windowed
changed-path pass. Redraw, resize, tab, theme, and cursor events consume
cached models.

### Treemap

Squarified layout via the `squarify` library:

- **Integer snapping**: float coordinates are snapped at endpoints (not
  widths) so adjacent rectangles share edges without gaps.
- **Adaptive padding**: top-level rectangles get a 1-cell label border when
  inner area ≥ 4×3; deeper levels skip padding.
- **Depth limiting**: typically 3 levels.
- **Minimum size**: rectangles below 1×1 render as a single cell.
- **Color**: category determines hue, depth modulates luminance (deeper =
  darker), directories take the dominance tint.
- **Diff mode**: area from target metric, diverging palette from normalized
  delta. Removed paths get bounded tombstone weight.
- **Large trees**: bounded top-N plus aggregate remainder; cursor-selected
  branch is retained even when tiny.

### Sunburst

Ring chart where each ring = one depth level, arc angle ∝ size:

- **Ring width**: `max_radius // (max_depth + 1)`.
- **Arc rendering**: a supersampled half-block pass — each half-cell
  averages four subsamples, and vertically adjacent halves that disagree
  become U+2580/U+2584.
- **Labels**: arcs > 30° at depth 1 get labeled; collision detection
  prevents overlaps.
- **Legend**: bottom-left, categories with byte shares.
- **Growth overlay**: diff frames replace category hue with delta state.
- **Narrow fallback**: canvases below 40×12 render a text summary.

**Frame cost**: `_rasterize_arcs` supersamples the pane four times per
half-cell, and what it asks the shape for — how far out the sample is, how
far around, what one radian is worth in ring edge there, how deep one cell
is — depends only on the offset from the centre and the band, never on the
tree. That was 97% of a frame (618k function calls for one 182×62 live
frame, 190k of them `ringshape._faces`), recomputed unchanged dozens of
times per scan. `viz/sunburst.py` now builds those answers once per
`(geometry, size, aspect, hole, ring width, radius)` into flat `array`
tables and keeps them LRU: 163 ms → 48 ms for that frame, 223 ms → 93 ms
for the full-depth one. The first frame at a new geometry pays for the
table (~165 ms at 182×62), which a live scan amortises over every frame
after it. A plan is 36 bytes a subsample — 3.25 MB at 182×62, 6.1 MB at
307×69 — so the cache is bounded by subsamples (400k, ~14 MB) as well as
by entries (4); the newest plan is always kept whatever its size.

The frames must be byte-identical to the uncached renderer, and a cache
whose key misses a parameter draws a wrong picture rather than raising, so
`tests/test_sunburst_cache.py` pins the framebuffer digest of 108
(tree, shape, size, aspect, depth) combinations taken from the renderer
before the cache existed, and re-renders the same matrix with the cache
switched off to check the key. `hit_test` is untouched: a mouse lookup
does its own arithmetic for one point rather than consulting either.

### Ring Shapes

`viz/ringshape.py` owns two functions — how far out a point is and how far
around — and swapping the pair produces different ring geometries:

| Shape | Radial | Angular |
|-------|--------|---------|
| `disc` | `hypot` | `atan2` |
| `fill` | Chebyshev distance | perimeter position |
| `tiles` | Cumulative area along the face | face position (straight-line cuts) |

`tiles` picks whole columns and half-rows first, so ring boundaries land on
the cell grid at any aspect — no calibration needed.
`tests/test_ring_shapes.py` verifies the claim by rasterizing against two
panel colors and counting partially-covered cells.

### Terminal Cell Geometry

Both charts do geometry in *units* where one unit = cell width, and a cell
is `cell_aspect` units tall. A disc of radius R is `2R` columns by
`2R / aspect` rows — a true circle on screen.

`resolve_cell_aspect()` returns a `CellAspect` with the ratio and its
provenance, resolved in order:

1. `DISKTIDE_CELL_ASPECT` env var
2. `[ui] cell_aspect` config
3. In-band resize report (terminal mode 2048)
4. `TIOCGWINSZ` pixel fields
5. Cached XTWINOPS probe (`CSI 16 t` / `CSI 14 t`)
6. 2.0 fallback

Everything is clamped to [1.5, 3.5]; any exception resolves to 2.0.

The XTWINOPS probe runs once in `__main__.cli()`, before `DiskTideApp` is
constructed, only when both stdio halves are ttys and no earlier layer
answered. It sends the probe queries plus DA1 in one write and reads until
DA1 replies or a 0.6s deadline — DA1 distinguishes "no answer is coming"
from "not yet." Replies must be consumed before Textual starts to prevent
them from appearing as garbage keystrokes.

`App.on_resize` feeds in-band reports to `report_pixel_size()`, which stores
the derived cell size (not window size) so it survives SIGWINCH at the same
font. A changed aspect bumps the render epoch rather than touching widgets
directly, because a layout must never be swapped mid-paint.

### Trend and Growth Heatmap

**TrendChart**: typed series, lines split at missing/removed/incompatible
points, root and subtree overlaid. Markers for partial confidence,
alert/anomaly events, pins, rollups, scan duration. Never converts absent
values to zero.

**GrowthHeatmap**: ≤ 16 intervals and ≤ 18 paths by default. Sort order:
consistency (positive intervals / total) and longest streak before peak
magnitude — repeated small growth outranks one spike. Lifecycle inference
treats absent rows as unchanged while the path exists, and missing before
creation or after removal. Narrow/safe/no-color rendering uses text summary
and state glyphs.

### Braille Canvas

`ColorBrailleCanvas` wraps `drawille.Canvas` with per-cell color tracking:

- Each cell maps to a 2×4 braille sub-pixel grid.
- Pixel colors are recorded per cell via voting — dominant color wins.
- `render_rows()` returns `(character, color)` tuples for Rich.
- Supports per-cell background colors and Bresenham line drawing.

### Live Scan Rendering

Tree, Sunburst, and Treemap update throughout a scan:

- `TreeScanScheduler` publishes directory checkpoints at most once per
  0.25s. Each update carries the COW root, changed nodes, and a bounded
  immutable `LiveViewNode` model.
- `SizeTree.apply_live_update()` updates materialized nodes in place; cursor
  and expanded state survive. The final tree reloads after deterministic
  accounting completes.
- Treemap and Sunburst consume the immutable model, limited to depth 2 and
  96 children per node. Excess children collapse into an aggregate "Other."
- Late updates from an older scan cannot replace a newer run.
- `live_scan_render = "auto"` (default) requires ≥ 80×24; otherwise
  progress-only mode. It is a legibility check, not a capacity one.

**The chart is duty-cycled; nothing else is.** The service coalesces
`NodeAggregateUpdated`, so the explorer is handed a frame exactly as fast
as it can draw one — and a chart frame is the most expensive thing on the
UI thread by two orders of magnitude (163 ms at a 182×62 sunburst before
the geometry cache, ~48 ms after; the size tree and the progress overlay
are ~2% of the thread between them). Every millisecond of it is spent
holding the GIL, so the scan threads behind it stall on each
re-acquisition: a 982k-entry local tree scanned in 21.0 s with the chart
off and 152.3 s with it on, and a 700k-entry home over NFS in 36.8 s
against ~420 s.

`SunburstView` / `TreemapView` time their own live frame
(`widgets.LivePaintCostMixin.last_paint_cost` — the layout plus the fold
into cells the first `render_line` would pay for), and
`ExplorerScreen._maybe_update_live_chart` forwards a frame only once
`_LIVE_CHART_DUTY` (5) times that has passed, floored at the scheduler's
own 0.25 s publish interval. That holds the chart to ~1/5 of the thread
at any widget size, where a constant would be wrong at both ends of a
33 ms (70×30) to 163 ms (182×62) range. A skipped frame arms one
`set_timer` for the rest of the gap and is replaced by the next, so a
scan that goes quiet still lands its last frame; completion always
paints. `tool/bench_scan.py --mode live --paint COLSxROWS` runs the same
loop on the service's dispatch thread without a terminal, and
`--paint-every-frame` reproduces what it looked like before (39.8 s / 37.8 s
/ 62.4 s on the same 88k-directory tree).

What is left is the rest of the live pipeline, not the chart: the same
307x69 A/B is 21.9 s off against 48.5 s on, and pinning the duty cycle so
the chart paints once still costs 40.4 s. The gap is the bounded view the
scheduler builds on every publish (~6 s on its own thread), the category
rollup worker (4-6 s), and the tree panel, the progress overlay and
Textual's compositor (~4 s on the UI thread) — each of them contending for
the same GIL the walk needs.

### Color Schemes

Five built-in schemes: `disktide` (default), `cold`, `colorblind`,
`cyberpunk`, `mono`.

A `ColorScheme` is a set of table keys: `name`, `label`, `category_key`,
`neutral_key`, `delta_key`, `textual_theme`, and treemap surface colors.
Every theme has two halves:

- **Charts** read `viz/colors.py` (no Textual imports — renderers run off
  the UI thread during live scans).
- **Chrome** (header, borders, footer) is a Textual `Theme` in
  `viz/chrome.py`, registered on `DiskTideApp.__init__`.

`DiskTideApp.apply_color_theme(name)` moves both halves: `set_color_scheme`
bumps the render epoch for charts, `App.theme` refreshes CSS for chrome.

The vocabulary — which keys exist, retired name resolution — lives in
`disktide/themes.py` (a zero-import module). `config.py` needs it but
`viz/colors.py` costs ~70ms to import, which is most of a `disktide scan`
that only reads config. `warm`, `default`, and `vivid` all resolve to
`disktide`.

**Category colors** are per-scheme. What is shared is the hue *order*:
`ephemeral` at the warm end and `docs`/`archive` at the cool end in all
chromatic themes. Seven content types were merged to six because that is the
ceiling at which pairwise hues stay distinguishable under colorblind
simulation. Colors come from precomputed OKLCH-derived tables in
`viz/colors.py`, generated by `tool/gen_palette.py` (single source of
truth).

`mono` carries `category_key = None`: six categories are a gray lightness
ladder; directories are never tinted.

**Ink roles**: styled text (tree, breadcrumb, progress, details, cleanup, FS
Overview) uses `viz.colors.ink(role)` with 15 semantic roles (`dir`, `file`,
`link`, `bar`, `warning`, `error`, `accent`, etc.) instead of hardcoded Rich
colors. Each ink is measured at ≥ 4.5:1 contrast against its theme's surface.
`mono`'s inks are asserted to have zero chroma.

**Diff palettes**: three diverging tables selected by `delta_key` — `default`
(shared by disktide/cold/cyberpunk), `colorblind` (blue/orange axis), and
`mono` (one diverging lightness ramp). `NO_COLOR` takes the `mono` table.

**Textual workarounds**:

- `OpaqueStripMixin` overlays the widget's `rich_style` background under
  `render_line` results to prevent SGR 49 leaking through to the terminal.
- Textual's `Tree` caches label `Text` objects at `set_label` time, so
  `refresh()` redraws old colors. `repaint_widgets` calls
  `rebuild_render_cache` before refreshing to re-theme without restart.

**Validation**: `tool/palette_checks.py` is a stdlib port of the six-checks
dataviz validator. `tool/gen_palette.py --check` prints measured numbers.
`tests/test_palette_gates.py` pins them — including byte-for-byte
reproduction, equality with shipped literal values, and theme
distinguishability.

## Screen Architecture

### App Startup Flow

```
disktide (no subcommand)
  → DiskTideApp(show_welcome=True)
  → on_mount: push WelcomeScreen
  → user picks path → dismiss(path)
  → _on_welcome_result
  → _launch_explorer: install explorer/cleanup/monitor/fs_overview/settings
  → push explorer, scan begins

disktide scan <path>
  → CLI-only, no TUI
```

Everything below the welcome screen is imported at first use:

- Services are lazy properties on `DiskTideApp`.
- Mode screens are imported inside `_launch_explorer()`.
- `_perform_quit` reads backing fields (`self.__scan_service`) instead of
  properties so quitting from the welcome screen doesn't build a service to
  discard it.

This dropped the pre-welcome import path from 563 modules to 406 and from
99 `disktide` modules to 27 (~0.15s warm, ~2.5s cold NFS savings).
`tests/test_startup_imports.py` enforces the boundary.

### Screen Management

Four mode screens (Explorer, Cleanup, Monitor, FS Overview) plus Settings
are installed (not pushed) after welcome, so they persist when switching with
`1`/`2`/`3`/`c`.

- `switch_screen()` swaps at the same stack level.
- `push_screen()` adds on top (used for settings).
- Explorer passes its root to Cleanup and cursor-highlighted path to Monitor.
- Visualization actions change path/snapshot context only — never mutate
  monitor definitions.

### FS Overview Loading

Uses platform adapter probes for mounts and block devices, `statvfs` for
capacity, optional `quota` output. Network `statvfs` calls run behind a
3-second watchdog. The `b` benchmark is the only write path (bounded temp
file, confirmed before writing).

## Dependencies

| Package | Purpose |
|---------|---------|
| textual ≥ 8.2, < 9 | TUI framework |
| textual-plotext ≥ 1.0, < 2 | Line chart plotting |
| squarify ≥ 0.4.0 | Treemap squarification |
| drawille ≥ 0.2.0 | Braille canvas |
| click ≥ 8.0 | CLI argument parsing |
| humanize ≥ 4.0 | Human-readable sizes/dates |
| inotify-simple ≥ 2, < 3 | Optional `[watch]` event acceleration |

Python ≥ 3.10 required (`slots=True` dataclasses, `X | Y` union syntax).
`tomllib` and `StrEnum` come from `disktide._compat` with backport fallbacks
on 3.10.

`uv.lock` is committed. CI tests 3.10–3.14 with `uv sync --locked`, then
installs the wheel into a clean environment. Budget: ≤ 20 runtime
distributions, ≤ 20 MiB, no native extension.
