# Architecture

Technical overview of fsmonitor's internals for anyone reading or extending the codebase.

## Project Layout

```
src/fs_monitor/
  __main__.py            CLI entry point (Click)
  app.py                 Textual App, screen management
  config.py              TOML config load/save, dataclasses
  glyphs.py              Unicode/ASCII glyph selection
  metrics.py             Size vs. file-count view metric helpers
  rendering.py           Process-wide safe-rendering state

  domain/
    alerts.py            Alert rule/event contracts
    cleanup.py           Persistent plan/action/identity/audit contracts
    delta.py             Compatibility decisions + shared delta result
    metrics.py           MetricId + StorageMeasurements semantics
    monitor.py           Definitions, status, history, retention contracts
    policy.py            Explicit ScanPolicy metadata
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
    snapshot.py          Pre-Wave05 compatibility imports
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
    sunburst.py          Ring chart via braille canvas
    braille.py           ColorBrailleCanvas -- per-cell color voting
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

All procfs, sysfs, mount-table, `lsblk`, and storage-medium probes live behind
`PlatformAdapter`. Probe failures are values (`available`, `degraded`, or
`unavailable`) with a reason and optional suggestion; they are not exceptions
that screens must catch. `scanner/sysinfo.py` and `scanner/blockdev.py` retain
their existing call signatures as compatibility facades while delegating I/O to
the active adapter.

`fsmonitor doctor` consumes the same capability snapshot as FS Overview. Its
versioned JSON output redacts application paths by default and does not enumerate
the user's scan tree.

## Scan Service and Event Protocol

`ScanService` is the product entry point for CLI scans, Explorer scans, cleanup
discovery, and foreground monitor hosts. It validates a
`ScanRequest`, resolves the active platform adapter and metric capability,
creates a stable `ScanRun` id, owns cancellation, and returns one normalized
terminal status: `completed`, `partial`, `cancelled`, or `failed`.

The domain contract in `domain/scan.py` has no Click, Textual, or SQLite imports.
Every event carries the same `run_id`, a contiguous `sequence`, and a `phase`.
The current protocol includes:

- `ScanStarted` and `ScanPhaseChanged`
- `DirectoryQueued`, `ScanProgressUpdated`, and `DirectoryCompleted`
- `NodeAggregateUpdated` for live/final tree handoff
- `AccessError` for explicit partial coverage
- exactly one terminal `ScanCompleted`, `ScanCancelled`, or `ScanFailed`

`ScanEventRecorder` can replay a captured run into `ProgressViewModel` and
`TreeViewModel`. Replay rejects mixed run ids, sequence gaps, and events after a
terminal event. Consumer callbacks are quarantined after their first exception;
the failure is recorded on `ScanRun.consumer_errors` and does not abort the
scanner or other consumers. A separate `ScanRunConsumer` seam receives the
normalized terminal run for future repository/persistence migration.

Each run dispatches through a bounded mailbox (64 pending events by default) on
one service-owned dispatcher thread. Pending progress, queued-directory,
completed-directory, and non-final tree events coalesce; terminal events do not.
The dispatcher drains pending items in batches and assigns sequence numbers only
at delivery, so journals remain contiguous after coalescing. `ScanRun` records
time-to-first-event, batch/coalesced counts, mailbox high-water mark, scheduler
queue bounds, time-to-first-visual, and cancellation latency. The Explorer
consumer only schedules `app.call_from_thread()`, so scanner and dispatcher
threads never mutate Textual widgets or view models.

See `docs/adr/0002-scan-service-event-protocol.md` and
`docs/adr/0003-all-tree-scheduler-and-event-backpressure.md` for the accepted
contracts.

## Scanner

### Threading Model

`collectors/local_scanner.py` adapts `ScanService` requests to `ScanEngine`, which
is now a compatibility facade over `TreeScanScheduler`. Every directory is one
non-recursive task: a worker scans direct entries and the coordinator lazily
materializes child-directory jobs across the whole tree. Executor submissions
are capped at two times the worker count by default. The coordinator pending
frontier has its own capacity and parent cursors create jobs only as slots open,
so neither the executor queue nor the coordinator deque can hold the full tree.

Direct scan completion installs zero-valued child-directory placeholders.
Descendant results update ancestor aggregates with contribution deltas. Live
roots use generation-based copy-on-write: once a root is published, later task
completion clones only modified directory paths, so old frames remain safe to
read. The recursive `scanner.walker.scan_directory()` and long-standing
`ScanEngine().scan(path)` APIs remain for diagnostics and compatibility tools;
product presentation code does not construct the engine directly.

The main Textual event loop stays on the main thread. Long-running scans use the
`@work(thread=True)` decorator. The screen submits a `ScanRun`, consumes typed
events, and uses `app.call_from_thread()` to marshal them back to the UI thread.
Monitor host events instead use Textual's non-blocking, thread-safe message
queue. This prevents the UI thread from joining a host worker that is itself
waiting for the UI event loop during shutdown.

### Adaptive Worker Count

`sysinfo.detect_system_info()` examines CPU count, load average, filesystem type (local vs network), storage type (HDD vs SSD), and available memory. Constraints:

- Network filesystems (NFS, CIFS, FUSE): capped at 4 workers
- HDD (rotational): capped at 4
- High system load: worker count reduced proportionally
- Low memory (<512 MB free): capped at 2
- Final range: 1--16 workers

When `workers` is set in config or CLI, the auto-detection is skipped.

### Progress Reporting

`ProgressThrottle` batches collector callbacks to a 100ms interval to avoid UI
thrashing. `ScanService` immediately copies each mutable callback payload into an
immutable `ScanProgressSnapshot` before publishing it. It tracks completed and
queued directories, queue depth, active workers, files, Logical bytes, current
and last-queued paths, elapsed time, and top-level completion data. A
`force_report()` call flushes collector state before the terminal event; queued
and completed deltas may therefore arrive as a batch without losing totals.

Progress reports Logical bytes because Unique requires global hardlink
reconciliation. The final tree additionally carries Allocated and Unique.

### Measurement and Scope Policy

- Logical is the existing `st_size` payload aggregate.
- Allocated is `st_blocks * 512` per visible file/symlink path.
- Unique assigns one deterministic lexical owner per `(st_dev, st_ino)`.
- Missing `st_blocks` remains unavailable rather than becoming zero.
- One-filesystem mode stops at device boundaries and keeps an `xdev` node.
- Descendant pseudo mounts are excluded by default; an explicit root is allowed.
- Max-depth and policy exclusions are separate from access errors.

See `docs/adr/0001-storage-metric-semantics.md` for the complete contract.

### Error Resilience

Each `os.scandir()` entry is wrapped in try/except. A permission error on one directory doesn't abort the scan -- the error is stored in `FSNode.error` and the scan continues with partial results.

### Symlink Handling

Symlinks are never recursed into: a symlink is stored as a leaf `FSNode` sized by the link itself (`lstat`), never its target. This prevents infinite loops and double-counting, and keeps a symlinked directory's bytes from inflating the parent total.

Target classification (the `os.readlink` for the target string and the `os.stat(follow_symlinks=True)` to learn whether the target is a directory, a file, or broken) is **deferred**: `make_symlink_node` pays only the one `entry.stat(follow_symlinks=False)` needed for the link's own size, and the deferred work runs in `classify_symlink`, called on demand by the Details panel render and the `i` action. The result is cached on the node via `link_classified`, so a second look is free. The engine eagerly classifies the first `_TOP_LEVEL_CLASSIFY_CAP = 100` symlinks at the scan root so the typical `fsmonitor ~` case shows target arrows in the tree from the start without re-introducing the per-symlink cost when the scan root itself contains hundreds of thousands of symlinks. Deeper symlinks remain fully lazy. This is what keeps the scan at one syscall per symlink on slow shared storage (cluster home, NFS, sshfs) where every extra round-trip is sub-millisecond but adds up.

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

Key behaviors:
- `sorted_children` -- lazy-cached sort by size descending
- `walk()` -- depth-first iteration over the entire subtree
- `walk_dirs()` -- depth-first iteration over directories only
- `find(path)` -- recursive path lookup
- `size_percent(parent_size)` -- percentage of parent

The model also carries `StorageMeasurements`, device/inode/link identity,
hardlink ownership, partial-access aggregates, lazy symlink classification,
filesystem-boundary/pseudo exclusion markers, max-depth truncation, and the
root `ScanPolicy`. Those fields let every presentation consume the same
semantics without re-walking the filesystem.

### Snapshot

`domain/snapshot.py` defines snapshot format v2 independently of the SQLite
schema. It records the stable scan result plus the metadata required to decide
whether a comparison is meaningful:

```python
@dataclass
class Snapshot:
    id: int | None
    root_path: str
    timestamp: datetime               # aware UTC for new snapshots
    total_size: int
    total_allocated_size: int | None
    total_unique_allocated_size: int | None
    file_count: int
    dir_count: int
    scan_duration: float
    format_version: int                # snapshot format, currently 2
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

Legacy rows remain readable. Missing policy fields are represented as unknown;
they are never filled with current defaults and presented as historical fact.

### SizeDelta

`domain/delta.py` is shared by CLI, Monitor, and later visualizations. A path
delta carries logical, allocated, unique, file-count, and directory-count
changes while retaining the pre-Wave05 `old_size`/`new_size` API:

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

`CompatibilityResult` separately records `compatible`,
`compatible-with-warning`, or `incompatible` plus field-level explanations and
recovery actions. `CompareResult` combines that decision with deterministic
path deltas and total changes.

## Snapshot Repository and Compare

Product code depends on `SnapshotRepository`, not SQLite. The protocol exposes
snapshot save/list/load, measurement reconstruction, history, deletion/pruning,
status, and read-only clone operations. `SQLiteSnapshotRepository` is the
current adapter; CLI and Textual screens receive it through the default factory.

`SnapshotService` converts a successful terminal `ScanRun` into canonical v2
metadata. `CompareService` resolves `latest`, `previous`, `oldest`, numeric ids,
and `--since` windows, then checks root identity, metric semantics/selection,
xdev, pseudo-filesystem, max-depth, symlink, hardlink, exclude, and partial
coverage metadata before loading measurements. Incompatible comparisons are
blocked unless the caller explicitly requests a raw/untrusted diff.

## Monitor Service and Foreground Hosts

`MonitorService` is the command/query boundary shared by Click commands and the
Textual Monitor Center. Presentation code submits create/update/pause/resume,
run-now, reconcile, host-session, archive, pin, retention, and alert intents; it does not
write SQLite or construct a second scheduler. Persistent definitions live in
the repository, while `config.toml` contains only global defaults, database
budgets, the TUI auto-start preference, and `monitor.event_mode`.

Three state dimensions remain separate:

- desired state: `enabled`, `paused`, or `archived`;
- activity: `no-host`, `waiting`, `queued`, `scanning`, `reconciling`, or `stopping`;
- health: `unknown`, `healthy`, `warning`, `failed`, or `blocked`.

An enabled definition does not imply background execution. The current TUI
process, `fsmonitor watch --monitor/--all`, and an externally supervised user
unit all acquire the same expiring repository lease and heartbeat it.
Start-to-start UTC due times are persisted, process waits use a monotonic clock,
one monitor never overlaps itself, and repeated run-now requests coalesce to one
pending rerun. The application ships a systemd user-unit example but does not
install or enable it.

Wave 10 inserts optional event acceleration into that host instead of creating a
second scheduler. `collectors.events` normalizes create/modify/delete/move,
overflow, root-lost, and backend-error signals. One backend and bounded
`DirtyPathTracker` attach to each held lease. Ordinary dirty subtrees are
rescanned through `ScanService` without writing a formal snapshot. Scheduled,
manual, startup/restart, and overflow recovery scans remain full root scans and
are the only runs that persist canonical history. Events that arrive during a
full scan stay dirty for the next reconciliation rather than being cleared by a
stale status object.

`monitor_status` exposes periodic/event-assisted mode, backend status, watched
root count, pending paths, last event/local/full reconciliation, overflow and
recovery counters, degraded reason, and confidence state. Backend stop, lease
expiry, root loss, watch-limit failure, or queue overflow cannot retain a stale
healthy projection; they require a later full reconciliation.

Every hosted run uses one pipeline: scan, persist snapshot, evaluate alerts,
apply retention when needed, record run/status, and publish monitor events.
`ScanService` serializes full scans shared by Explorer and Monitor so competing
product surfaces do not start simultaneous tree walks.

Retention keeps recent snapshots densely, promotes older history into
hourly/daily/weekly representatives, preserves pinned snapshots and the newest
snapshot in every monitor revision, and records source range/count provenance.
Deleting a required baseline first materializes and promotes the earliest
survivor. The global soft budget triggers maintenance; the hard budget attempts
maintenance and then blocks only snapshot persistence, not the in-memory scan.

Alert rules are persisted per monitor and evaluated from canonical snapshot
measurements. Supported rules cover absolute size/growth, percentage growth,
free bytes, free inodes, and newly appearing large items. Events record old/new
snapshot ids, observed and threshold values, severity, confidence, cooldown
suppression, and suppression reason. Partial snapshots create suppressed audit
events instead of presenting low-confidence triggers as definitive.

See `docs/adr/0005-monitor-service-retention-and-alerts.md` for the base host,
scheduling, retention, and alert boundaries, and ADR 0009 for event acceleration.

## Database

SQLite with WAL mode, stored at `~/.local/share/fsmonitor-cli/data.db` (respects `XDG_DATA_HOME`; the legacy directory name is retained for upgrade compatibility).

### Schema

Database schema v7 remains distinct from snapshot format v2 and the public
snapshot API version.

**monitor_definitions** -- Canonical path, label, revision, desired state,
start-to-start interval, selected metric, serialized scan policy, workers, and
versioned retention policy.

**monitor_status / monitor_leases** -- Activity/health projection, next due and
last run details, active phase/progress, failure state, retention summary,
event/backend/dirty/reconciliation confidence, and the current foreground host
lease/heartbeat.

**monitored_roots** -- Stable root path plus observed device, inode, filesystem,
and last-seen timestamp.

**scan_runs** -- Run id, lifecycle timestamps/status, duration, adapter/version,
selected metric, serialized policy, coverage counts, monitor/revision, trigger,
scheduled time, host, and resulting snapshot.

**snapshots** -- Stable legacy-compatible totals plus baseline/delta links.

**snapshot_metadata** -- Snapshot format/API versions, metric semantics and
availability, policy, scanner/run/root identity, completion/coverage fields,
timezone rule, capabilities, legacy inference source, monitor/revision, and
rollup kind.

**paths** -- Interned file and directory paths with parent, basename, and depth.

**nodes** -- Full file/directory measurements for baseline snapshots: logical,
allocated, unique, own measurements, counts, mtime, error, and node kind.

**deltas** -- Changed, added, or removed file/directory state for non-baseline
snapshots, using the same measurements plus `is_removed`.

**snapshot_pins / snapshot_rollups / retention_runs** -- Explicit protection,
rollup provenance, and auditable maintenance before/after counts and bytes.

**alert_rules / alert_events** -- Version-two rule configuration and immutable
trigger/suppression audit. The legacy `deletion_log` API table and
`schema_version` also remain.

**cleanup_plans / cleanup_actions / cleanup_audit** -- Schema-v6 persistent
plan payloads, per-action state/identity indexes, and immutable validation,
execution, and undo events.

Dynamic path/id lookups are split into bounded SQLite bind batches. Snapshot
save/load, tree reconstruction, retention pruning, and cleanup-action pruning do
not generate SQL whose placeholder count grows with the scanned tree or plan.

The first snapshot for a root is a baseline; another full baseline is stored every 50 snapshots. Intermediate snapshots compare against the previously resolved state and persist only changed directory rows. When retention deletes a baseline, the earliest surviving dependent is materialized and promoted before the old baseline is removed.

### Snapshot Queries

`list_snapshots(root_path)` uses bidirectional matching by default: given `/a/b`, it finds exact, ancestor, and descendant watch roots. `strict_path=True` restricts this to exact matches.

`load_tree()`, `load_measurements()`, and `compare_snapshots()` remain explicit
full-state APIs. Interactive history and visualization use targeted contracts:
`load_measurement_series()` resolves only requested paths across an ordered
snapshot window, `list_changed_paths()` streams exact metric-ranked interval
candidates, and `load_visualization_projection()` constructs paired sparse
trees with selected ancestors and exact aggregate remainder nodes.

`get_size_history(path)` combines baseline rows and deltas, then forward-fills
unchanged snapshots to return `(timestamp, size)` pairs. Monitor history queries
add revision, pin, rollup, partial, missing, removed, and incompatible state so
the TUI never converts an absent subtree into a false zero.

### Migration and degraded behavior

Before changing a non-empty on-disk database, migration writes a SQLite backup
next to it (for the current schema v7: `data.db.pre-v7.bak`). All DDL, backfill, and schema
version changes run in one transaction; failure rolls back without advancing
`schema_version`. Existing schema-v3/v0.1.7 snapshots are marked legacy with an
explicit inference source rather than discarded. Schema-v5 migration also maps
the old size/percentage alert prototype into the new rule/event audit fields;
schema v6 adds CleanupPlan/audit tables and schema v7 adds event-assisted monitor
status without changing snapshot format v2.

If migration or writes fail but the database is readable, the adapter opens the
original read-only so list/history remain available. If the file is corrupt or
cannot be read, the app uses an in-memory degraded repository for scan-only use.
It never deletes or overwrites the user's database as an automatic repair.

## Cleanup System

### Rule Matching

`extensions/cleanup_rules.py` validates schema-v1 TOML and builds one catalog
from packaged and user-owned packs. Pack loading fails independently: malformed
TOML, unknown fields, unsupported versions, invalid identifiers/types, and name
collisions become doctor-visible issues without hiding valid packs. The schema
contains no executor or arbitrary code field. User policy loads from
`~/.config/fsmonitor-cli/cleanup-rules/*.toml`; disabled pack names come from
`cleanup.disabled_rule_packs` and are shared by CLI and TUI.

`detect_targets(root)` walks the FSNode tree and tests each node against the enabled rule list. A rule matches when:

1. The node's name matches one of the rule's glob patterns
2. If `parent_indicators` is set, at least one indicator file exists in the node's parent directory
3. If `path_context` is set, the candidate path matches that contextual glob
4. If `min_age_days` is set, the node's mtime is older than the threshold

### Built-in Rule Packs

| Pack | Main coverage | Default policy |
|------|---------------|----------------|
| `python` | bytecode/tool caches, virtualenvs, package builds | safe/preview by rule |
| `node` | dependencies, frontend builds, project tool caches | safe |
| `rust` | Cargo `target` with `Cargo.toml` indicator | safe |
| `general` | aged logs/temp and OS metadata | safe |
| `ide` | `.idea` / `.vscode` project metadata | preview |
| `containers` | project-local Docker/BuildKit-style cache paths | detection-only |

`cleanup/scoring.py` ranks candidates with a documented 0–100 formula: 35%
logarithmic size, 25% age, 20% inverse risk, 10% rebuildability, and 10% rule
confidence. Partial, inaccessible, and overlapping evidence only lowers the
separate confidence value. Score never authorizes an action.

### Candidate → CleanupPlan

`detect_targets()` only emits candidate facts. `CleanupService.create_plan()`
captures `lstat` identity, rule and pack provenance, pack/schema versions,
source, path context, rule policy, risk, age, score, confidence/coverage,
logical reclaim estimate, and scan reference. It resolves ancestor/descendant
overlap before persistence; a parent action subsumes its children so bytes and
execution are counted once. CleanupPlan JSON payload v2 is stored in the
existing schema-v6 plan/action tables, so Wave 09 adds no database migration.
Legacy plan payloads remain readable. The default CLI and TUI action is
`preview`, which persists the plan/audit records but does not mutate the
filesystem.

### Revalidation and protected paths

Before each action, `CleanupService` repeats device/inode/mode/mtime/size checks,
re-measures directory contents, and rechecks rule name, indicators, age, current
risk, scan-root containment, mount boundaries, and application-protected paths.
`/`, the scan root, mount roots, the database directory, and quarantine roots
fail closed. Replacing a target with a symlink is stale; an original symlink is
handled as the link itself and is never followed.

### Safe executors and undo

Normal apply first attempts an atomic same-filesystem Freedesktop Trash rename.
If Trash is unavailable for that target, `QuarantineExecutor` creates an owned
mode-0700 sibling directory, writes a recovery manifest, enforces capacity and
expiry policy, and performs `os.rename()` without cross-filesystem copy/delete.
Both paths persist undo metadata and refuse to overwrite a newly created
original path. Isolation reports actual reclaimed bytes as zero. Permanent
deletion remains a low-level primitive reachable only after the exact
`DELETE <plan-id>` confirmation. Detection-only rules are blocked by every
generic safe/permanent execution path.

Every validation, pre-execution intent, result, and undo is appended to
`cleanup_audit`. If the pre-execution audit write fails, no filesystem action is
attempted and the remaining batch stops. `cleanup/actions.py::delete_targets()`
remains only as a legacy low-level compatibility helper; no CLI or TUI product
path calls it.

### Map, savings history, purge, and alerts

`widgets/cleanup_map.py` builds a deterministic top-N Age/Size model, caches it
by candidate set and dimensions, and synchronizes selected paths with the
Cleanup table. Narrow or safe-rendering terminals receive a bounded fallback
list. `cleanup.map_max_points` constrains the model to 10–500 points.

Savings history is derived from persisted plans/actions and keeps estimated,
isolated, purged, actual reclaimed, and undone bytes separate. Quarantine purge
revalidates the manifest/identity and requires `PURGE <plan-id>`; system Trash
is outside the purge contract. Cleanup-opportunity alerts read a persisted plan
summary, include top categories/estimate/confidence plus a preview command, and
only notify. They never create or execute a plan and use a distinct Trend
marker.

## Visualization

### Space-Time Contracts

`domain/visualization.py` is the only semantic classifier used by Wave 07.
`VisualState` distinguishes new, removed, growth, shrink, unchanged, partial,
incompatible, and missing data. `DiffFrame`, typed Trend points/series, and
Growth Heatmap intervals retain path identity, metric, confidence, and snapshot
ids. Widgets never compare snapshots or query SQLite.

`VisualizationService` consumes `MonitorHistory` and the `SnapshotRepository`
protocol. It blocks incompatible pairs before projection, caches sparse diff
frames and bounded path-series results, and builds Heatmap intervals from one
windowed changed-path candidate pass instead of cloning historical trees.
Explorer requests latest/previous or an adjacent pair only after scan
stabilization; Monitor loads all four History views in its existing background
worker. Redraw, resize, tab, theme, and cursor events consume cached models.

The rendering-neutral `visualization_formatting.py` module owns shared labels,
glyphs, delta formatting, sparklines, and legends. The old TUI view-model module
is a compatibility re-export only. Safe rendering substitutes ASCII glyphs;
`NO_COLOR` uses grayscale backgrounds while preserving the same state tokens.

Both spatial charts size their areas by a selectable *metric*: total bytes (the default) or file count. `compute_layout` and `compute_sunburst` take a `metric` argument, and the size tree, treemap, sunburst, and Details panel all read it so a toggle (`t` in the explorer) keeps every view consistent. `fs_monitor/metrics.py` centralises the vocabulary: `metric_value()` selects the FSNode field and `metric_text()` formats it. Both fields are aggregated bottom-up during the scan, so switching is a re-layout of in-memory data with no extra filesystem work.

### Treemap

Uses the `squarify` library for squarified layout. Key implementation details:

- **Integer snapping**: `squarify` returns float coordinates. The renderer snaps rectangle *endpoints* (not widths) to integer grid boundaries so adjacent rectangles share edges without gaps or overlaps.
- **Adaptive padding**: Top-level rectangles get a 1-cell border for labels when there's room (inner area >= 4x3). Deeper levels skip padding to preserve space.
- **Depth limiting**: Typically 3 levels deep to prevent visual clutter.
- **Minimum cell size**: Rectangles that collapse below 1x1 are still rendered as a single cell rather than disappearing.
- **Coloring**: File-type category determines hue (from the active color scheme), depth modulates luminance (deeper = darker), and directories get distinct border colors.
- **Diff mode**: Area uses the target/current metric, while the shared diverging palette uses normalized delta. Removed paths receive bounded tombstone weight.
- **Large trees**: Each viewport lays out at most a bounded top-N plus one aggregate remainder. The cursor-selected branch is retained even when it is tiny.

### Sunburst

Ring chart where each concentric ring represents a depth level, and arc angles are proportional to size.

- **Ring width**: `max_radius // (max_depth + 1)`, giving roughly equal thickness per ring.
- **Arc rendering**: `ColorBrailleCanvas.fill_arc()` fills ring segments densely by sampling many radii per arc.
- **Labels**: Arcs wider than 30 degrees at depth 1 get labeled. A collision detection pass prevents overlaps.
- **Legend**: Bottom-left shows file-type categories with their colors.
- **Growth overlay**: Diff frames replace category hue with shared delta state while preserving path/ring identity. Selected branches remain in the arc model below the normal tiny-arc cutoff.
- **Narrow fallback**: Canvases below 40x12 render a readable summary and legend instead of a clipped ring chart.

### Trend and Growth Heatmap

`TrendChart` accepts typed series, splits lines at missing/removed/incompatible
points, overlays root and selected subtree, and exposes cached zoom/pan windows.
Markers identify partial confidence, alert/anomaly events, pins, rollups, and
scan duration without converting absent values into zero.

`GrowthHeatmap` receives at most 16 intervals and 18 ranked paths by default.
Consistency (positive intervals / compatible intervals) and longest streak sort
before peak magnitude, so repeated small growth outranks one isolated spike.
Lifecycle inference treats absent changed rows as unchanged while the path
exists, and as missing before creation or after removal. Narrow/safe/no-color
rendering uses a text summary and state glyphs.

### Braille Canvas

`ColorBrailleCanvas` wraps `drawille.Canvas` to add per-cell color tracking:

- Each terminal character cell maps to a 2x4 braille sub-pixel grid.
- When pixels are set, their colors are recorded per cell via a voting system. The dominant color (most pixels set with that color) wins.
- `render_rows()` converts the canvas to `(character, color)` tuples for Rich rendering.
- Also supports per-cell background colors and line drawing via Bresenham's algorithm.

### Live Scan Rendering

Tree, Sunburst, and Treemap update throughout a scan instead of waiting for the
terminal tree:

- `TreeScanScheduler` publishes directory checkpoints at most once per 0.25 s.
  Each `ScanTreeUpdate` carries the COW root, changed nodes, newly stable paths,
  and a bounded immutable `LiveViewNode` model.
- `SizeTree.apply_live_update()` updates changed materialized nodes in place.
  Cursor path and expanded branches survive updates; the final tree is reloaded
  once deterministic accounting completes.
- Treemap and Sunburst consume the immutable model, limited to depth 2 and 96
  children per node. Excess children collapse into an aggregate “Other” node,
  so live rendering does not copy the complete mutable `FSNode` graph.
- The Explorer filters events by active run id. Late updates from an older scan
  cannot replace a newer run. In-flight drill/navigation is blocked with an
  explicit notification because aggregates below unfinished nodes can change.
- `ui.live_scan_render` remains `auto`, `on`, or `off`. `auto` requires at least
  an 80x24 canvas and four CPUs; lower-resource sessions use progress-only mode.
- In live mode the progress surface docks above the incremental tree. In
  progress-only mode it owns the tree panel until completion. The visualization
  panel remains unobstructed in both modes.

### Color Schemes

Five built-in schemes: `default`, `cold`, `warm`, `vivid`, `mono`. Each scheme defines:

- Size category thresholds with Rich color names
- Depth hue rotation cycle (8 levels)
- File-type category hue mapping (code, document, image, data, model, config, media, archive, build, log, other)
- Gradient hue range for ratio-based coloring
- Directory and border colors

Helper functions: `file_category(name)` maps extensions to categories, `depth_color()` and `gradient_color()` produce HSL-derived colors, `hsl_to_rgb()` handles color space conversion.

## Screen Architecture

### App Startup Flow

```
fsmonitor (no subcommand)
  -> FSMonitorApp(show_welcome=True)
  -> on_mount: push WelcomeScreen
  -> user picks path -> dismiss(path)
  -> _on_welcome_result callback
  -> _launch_explorer: install explorer/cleanup/monitor/fs_overview/settings screens
  -> push explorer screen, scan begins

fsmonitor scan <path>
  -> CLI-only, no TUI
```

### Screen Management

`FSMonitorApp` installs four mode screens (Explorer, Cleanup, Monitor, FS Overview) plus Settings after the welcome screen completes. Screens are installed (not pushed) so they persist when switching among Explorer/Monitor/FS Overview with `1`/`2`/`3`; Cleanup remains on `c` because it is disabled by default and has its own plan/apply workflow.

- `switch_screen()` swaps the current screen at the same stack level
- `push_screen()` adds a screen on top (used for settings overlay)
- Data flows between screens: Explorer passes its scanned root to Cleanup and its cursor-highlighted path to Monitor; visualization actions only change path/snapshot navigation context and never mutate monitor definitions.

### Monitor Refresh

The monitor screen loads data both on first mount (`on_mount`) and every time it becomes the active screen (`on_screen_resume`). This ensures fresh data from an ongoing `watch` process is always visible.

### FS Overview Loading

FS Overview requests mount and block-device probes from the active platform
adapter, uses `statvfs` for capacity, and optionally reads user quota output.
Unavailable probes stay visible with their reason. Network `statvfs` calls run
behind a 3-second watchdog so a stale NFS/CIFS mount cannot freeze the screen.
The `b` action is the only write path: after confirmation it creates and removes
a bounded temporary benchmark file on the selected mount.

## Dependencies

| Package | Purpose |
|---------|---------|
| textual >= 8.2, < 9 | TUI framework |
| textual-plotext >= 1.0, < 2 | Line chart plotting |
| squarify >= 0.4.0 | Treemap squarification algorithm |
| drawille >= 0.2.0 | Braille canvas drawing |
| click >= 8.0 | CLI argument parsing |
| humanize >= 4.0 | Human-readable sizes and dates |
| inotify-simple >= 2, < 3 | Optional Linux `[watch]` event acceleration |

Python >= 3.11 required (uses `tomllib`, `slots=True` dataclasses, `X | Y` union syntax).

`uv.lock` is committed. CI tests Python 3.11, 3.12, and 3.13 with
`uv sync --locked`, then installs the wheel into a clean environment. The core
budget is at most 20 runtime distributions and 20 MiB with no native extension.
CI separately installs the `watch` extra and verifies event backend discovery;
the minimal environment verifies that the same capability remains unavailable
without affecting periodic monitoring.
