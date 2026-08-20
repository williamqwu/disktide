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
    metrics.py           MetricId + StorageMeasurements semantics
    policy.py            Explicit ScanPolicy metadata
    scan.py              ScanRequest/ScanRun/status/event contracts

  collectors/
    local_scanner.py     ScanEngine adapter used by ScanService
  collectors/platform/
    base.py              Portable adapter + shared capability probes
    linux.py             procfs/sysfs/lsblk implementation
    portable.py          Conservative macOS/Windows/unknown adapters
    models.py            Mount, block-device, and ProbeResult data

  extensions/
    capabilities.py      CapabilityId/status/reason public vocabulary

  services/
    doctor.py            Human + JSON installation diagnostics
    scan.py              Run lifecycle, cancellation, event dispatch
    scan_consumers.py    Progress/tree view models and event replay

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
    snapshot.py          Snapshot metadata, SizeDelta for diffs
    patterns.py          CleanupRule, CleanupTarget, RiskLevel

  storage/
    database.py          SQLite backend (WAL mode)
    migrations.py        Schema versioning

  cleanup/
    detector.py          Walk tree and match against rules
    rules.py             8 built-in cleanup rules
    actions.py           Permanent deletion with dry-run support

  monitor/
    alerts.py            Threshold alert rules and event checking
    diff.py              Tree-level snapshot comparison (SizeDelta)
    scheduler.py         Interval-based periodic scan scheduling

  viz/
    treemap.py           Squarified treemap layout + rendering
    sunburst.py          Ring chart via braille canvas
    braille.py           ColorBrailleCanvas -- per-cell color voting
    colors.py            5 color schemes, HSL utilities

  screens/
    welcome.py           Welcome screen with path completion
    explorer.py          Tree + visualization (treemap/sunburst/details)
    cleanup.py           Target table + deletion workflow
    monitor.py           Snapshot history + trend chart
    fs_overview.py       Mounted filesystems, block devices, benchmark
    settings.py          Configuration editing

  widgets/
    size_tree.py         Tree[FSNode] with size bars, lazy loading
    breadcrumb.py        Path breadcrumb navigation
    info_panel.py        Details for selected node
    treemap_view.py      Treemap Textual widget
    sunburst_view.py     Sunburst Textual widget
    trend_chart.py       Historical size line chart
    scan_progress.py     Scan progress overlay
    cleanup_modal.py     Deletion confirmation dialog
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
discovery, periodic watch scans, and the monitor scheduler. It validates a
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

Captures a point-in-time scan result:

```python
@dataclass
class Snapshot:
    id: int | None
    root_path: str         # resolved absolute path
    timestamp: datetime
    total_size: int
    file_count: int
    dir_count: int
    scan_duration: float
    label: str
    is_baseline: bool
    baseline_id: int | None
```

### SizeDelta

Result of comparing two snapshots at the same path:

```python
@dataclass
class SizeDelta:
    path: str
    old_size: int
    new_size: int
    is_new: bool           # appeared in new snapshot
    is_removed: bool       # gone from new snapshot
```

Properties `delta`, `growth_percent`, `is_growth`, `is_shrink` are derived.

## Database

SQLite with WAL mode, stored at `~/.local/share/fsmonitor-cli/data.db` (respects `XDG_DATA_HOME`; the legacy directory name is retained for upgrade compatibility).

### Schema

The current schema (v3) interns directory paths and stores periodic full baselines plus per-snapshot deltas. Files are represented in directory aggregates; snapshot persistence does not store one row per file.

**snapshots** -- One metadata row per scan. In addition to root path, timestamp, totals, duration, and label, `is_baseline` marks full snapshots and `baseline_id` links delta snapshots to their baseline.

**paths** -- One row per unique directory path, with `parent_id`, basename, and depth. Reusing path IDs prevents identical path strings from being duplicated across snapshots.

**nodes** -- Full directory state for baseline snapshots only: `snapshot_id`, `path_id`, size, own size, file/dir counts, mtime, and error.

**deltas** -- Changed, added, or removed directory state for non-baseline snapshots. It mirrors the node metrics and adds `is_removed`.

**Other tables:** `alert_rules`, `alert_events`, the legacy `deletion_log` API table, and `schema_version`.

The first snapshot for a root is a baseline; another full baseline is stored every 50 snapshots. Intermediate snapshots compare against the previously resolved state and persist only changed directory rows. When retention deletes a baseline, the earliest surviving dependent is materialized and promoted before the old baseline is removed.

### Snapshot Queries

`list_snapshots(root_path)` uses bidirectional matching by default: given `/a/b`, it finds exact, ancestor, and descendant watch roots. `strict_path=True` restricts this to exact matches.

`load_tree()` and `compare_snapshots()` reconstruct each requested state by loading its baseline and applying ordered deltas. Comparison then operates on the two resolved path-ID maps and returns `SizeDelta` objects for changed, added, or removed directories. A `min_delta` threshold filters out noise.

`get_size_history(path)` combines baseline rows and deltas, then forward-fills unchanged snapshots to return `(timestamp, size)` pairs.

## Cleanup System

### Rule Matching

`detect_targets(root)` walks the FSNode tree and tests each node against the rule list. A rule matches when:

1. The node's name matches one of the rule's glob patterns
2. If `parent_indicators` is set, at least one indicator file exists in the node's parent directory
3. If `min_age_days` is set, the node's mtime is older than the threshold

### Built-in Rules

| Rule | Patterns | Risk | Category |
|------|----------|------|----------|
| node_modules | `node_modules` | Safe | dependencies |
| python_cache | `__pycache__`, `.pytest_cache`, `.mypy_cache` | Safe | cache |
| python_bytecode | `*.pyc`, `*.pyo` | Safe | cache |
| build_outputs | `build`, `dist`, `.next` | Moderate | build |
| rust_target | `target` | Moderate | build |
| old_logs | `*.log` (>30 days) | Safe | logs |
| system_junk | `.DS_Store`, `Thumbs.db`, `desktop.ini` | Safe | junk |
| ide_caches | `.idea`, `.vscode` | Moderate | ide |

### Deletion

`delete_targets()` is a direct, permanent filesystem action: directory symlinks are unlinked as links, real directories use `shutil.rmtree()`, and files use `os.unlink()`. A `dry_run` mode reports the result without changing the filesystem, and failed deletions are collected without aborting the batch. The current cleanup flow does not revalidate stale targets, use trash/quarantine, provide undo, or write the legacy `deletion_log` table.

## Visualization

Both spatial charts size their areas by a selectable *metric*: total bytes (the default) or file count. `compute_layout` and `compute_sunburst` take a `metric` argument, and the size tree, treemap, sunburst, and Details panel all read it so a toggle (`t` in the explorer) keeps every view consistent. `fs_monitor/metrics.py` centralises the vocabulary: `metric_value()` selects the FSNode field and `metric_text()` formats it. Both fields are aggregated bottom-up during the scan, so switching is a re-layout of in-memory data with no extra filesystem work.

### Treemap

Uses the `squarify` library for squarified layout. Key implementation details:

- **Integer snapping**: `squarify` returns float coordinates. The renderer snaps rectangle *endpoints* (not widths) to integer grid boundaries so adjacent rectangles share edges without gaps or overlaps.
- **Adaptive padding**: Top-level rectangles get a 1-cell border for labels when there's room (inner area >= 4x3). Deeper levels skip padding to preserve space.
- **Depth limiting**: Typically 3 levels deep to prevent visual clutter.
- **Minimum cell size**: Rectangles that collapse below 1x1 are still rendered as a single cell rather than disappearing.
- **Coloring**: File-type category determines hue (from the active color scheme), depth modulates luminance (deeper = darker), and directories get distinct border colors.

### Sunburst

Ring chart where each concentric ring represents a depth level, and arc angles are proportional to size.

- **Ring width**: `max_radius // (max_depth + 1)`, giving roughly equal thickness per ring.
- **Arc rendering**: `ColorBrailleCanvas.fill_arc()` fills ring segments densely by sampling many radii per arc.
- **Labels**: Arcs wider than 30 degrees at depth 1 get labeled. A collision detection pass prevents overlaps.
- **Legend**: Bottom-left shows file-type categories with their colors.

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

`FSMonitorApp` installs four mode screens (Explorer, Cleanup, Monitor, FS Overview) plus Settings after the welcome screen completes. Screens are installed (not pushed) so they persist when switching modes with `E`/`C`/`M`/`F`.

- `switch_screen()` swaps the current screen at the same stack level
- `push_screen()` adds a screen on top (used for settings overlay)
- Data flows between screens: explorer's scanned root node is passed to cleanup when switching modes

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

Python >= 3.11 required (uses `tomllib`, `slots=True` dataclasses, `X | Y` union syntax).

`uv.lock` is committed. CI tests Python 3.11, 3.12, and 3.13 with
`uv sync --locked`, then installs the wheel into a clean environment. The core
budget is at most 20 runtime distributions and 20 MiB with no native extension.
