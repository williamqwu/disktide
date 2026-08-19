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

  scanner/
    benchmark.py         Opt-in mount throughput probe
    blockdev.py          lsblk-backed block-device inventory
    engine.py            Multi-threaded scan orchestrator
    walker.py            os.scandir()-based recursive walker
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

## Scanner

### Threading Model

`ScanEngine.scan(path)` scans the root directory entries on the main call, then dispatches each top-level subdirectory to a `ThreadPoolExecutor`. Each thread recursively walks its subtree with `os.scandir()` and returns an `FSNode` tree. The engine aggregates results bottom-up.

The main Textual event loop stays on the main thread. Long-running scans use the `@work(thread=True)` decorator, with `app.call_from_thread()` to marshal progress updates back to the UI thread.

### Adaptive Worker Count

`sysinfo.detect_system_info()` examines CPU count, load average, filesystem type (local vs network), storage type (HDD vs SSD), and available memory. Constraints:

- Network filesystems (NFS, CIFS, FUSE): capped at 4 workers
- HDD (rotational): capped at 4
- High system load: worker count reduced proportionally
- Low memory (<512 MB free): capped at 2
- Final range: 1--16 workers

When `workers` is set in config or CLI, the auto-detection is skipped.

### Progress Reporting

`ProgressThrottle` batches callbacks to a 100ms interval to avoid UI thrashing. It tracks dirs scanned, files scanned, total size, and current path. A `force_report()` call flushes immediately on scan completion.

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

The model also carries partial-access aggregates, lazy symlink classification fields, and a bind-mount/cycle marker. Those fields let the UI surface incomplete scans without re-walking the tree.

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

The Sunburst and Treemap tabs can draw themselves as the scan runs, so the user sees the result form ring by ring (or rect by rect) instead of staring at an indeterminate bar for the duration. The mechanism is intentionally small:

- `ScanEngine` accepts an optional `tree_callback: Callable[[FSNode], None]` and a `tree_callback_interval` (default 0.25 s). The engine fires the callback once after the top-level `scandir` (force), then at most once per interval as each top-level subdir future resolves, and unconditionally one final time at the end. The first non-forced emit after a force bypasses the throttle so the first ring slice appears as soon as one top-level subdir lands rather than after a full interval of empty viz. Each emit is a fresh shallow-copy `FSNode` built on the engine thread, so the UI thread reads an immutable handoff.
- `_roll_up()` on the engine is the single source of truth for aggregate math (`size`, `file_count`, `dir_count`, denied/partial subtree counts). Both the live snapshot path and the final `_finalize_root()` call it, so the numbers cannot drift between mid-scan and end-of-scan reads.
- The explorer's `_apply_tree_snapshot()` pushes the snapshot to whichever viz tab is currently active (`_current` is tracked across snapshots so a mid-scan tab switch picks up the latest). `SunburstView.set_live_mode(True)` and `TreemapView.set_live_mode(True)` swap `max_depth` to 2 for the in-flight frames (vs. 4 / 3 normally), which cuts each braille fill to a small fraction of its normal cost and stabilises the picture (outer rings re-tile every time a subtree's size lands). Full depth is restored on completion.
- Drill-into is gated by `_scan_in_progress`: `u`, `i`, `r`, and tree-click-to-drill all return early until the final snapshot arrives. The per-subtree aggregates inside a live snapshot are honest, but the root totals are not, and the Details panel must not show numbers that contradict themselves a second later.
- The user-facing toggle is `ui.live_scan_render`: `auto` (default), `on`, or `off`. `resolve_live_scan_render()` in `config.py` resolves `auto` against the Textual app's canvas size (passed in by the explorer at scan start) and `os.cpu_count()`: live mode is on only when the canvas is at least 80 columns by 24 rows AND at least 4 CPUs are available. Below that, the per-frame cost is measurable against the scan and the chart has no room to be visible around the 60x12 progress overlay, so we silently fall back to the v0.1.5 behavior. The Settings screen exposes the same dropdown.
- The progress overlay lives inside `#tree-panel` (a sibling of `SizeTree`) and only shows during a scan, via a `.scanning` CSS class on the tree-panel that hides the tree + sort indicator and unhides the overlay in their place. The tree is empty mid-scan anyway, so the panel real estate gets put to good use AND the viz panel on the right is left entirely free for the live render. (Earlier v0.1.6 iterations floated the overlay centered: first via a full-screen wrapper with `background: transparent`, which occluded the viz because Textual treats transparent-bg widgets as owning their cells; then via `position: absolute` on the overlay itself, which fixed the occlusion but still ate the center of the viz panel.)

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

FS Overview reads `/proc/mounts`, uses `statvfs` for capacity, optionally reads user quota output, and queries `lsblk` for the block-device tree. Network `statvfs` calls run behind a 3-second watchdog so a stale NFS/CIFS mount cannot freeze the screen. The `b` action is the only write path: after confirmation it creates and removes a bounded temporary benchmark file on the selected mount.

## Dependencies

| Package | Purpose |
|---------|---------|
| textual >= 1.0.0 | TUI framework |
| textual-plotext >= 0.2.0 | Line chart plotting |
| squarify >= 0.4.0 | Treemap squarification algorithm |
| drawille >= 0.2.0 | Braille canvas drawing |
| click >= 8.0 | CLI argument parsing |
| humanize >= 4.0 | Human-readable sizes and dates |

Python >= 3.11 required (uses `tomllib`, `slots=True` dataclasses, `X | Y` union syntax).
