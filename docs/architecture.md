# Architecture

Technical overview of fsmonitor-cli's internals for anyone reading or extending the codebase.

## Project Layout

```
src/fs_monitor/
  __main__.py            CLI entry point (Click)
  app.py                 Textual App, screen management
  config.py              TOML config load/save, dataclasses
  metrics.py             Size vs. file-count view metric helpers

  scanner/
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
    cache.py             mtime-based JSON scan cache
    migrations.py        Schema versioning

  cleanup/
    detector.py          Walk tree and match against rules
    rules.py             8 built-in cleanup rules
    actions.py           Deletion with dry-run + audit logging

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
- Low memory (<1 GB free): capped at 2
- Final range: 1--16 workers

When `workers` is set in config or CLI, the auto-detection is skipped.

### Progress Reporting

`ProgressThrottle` batches callbacks to a 100ms interval to avoid UI thrashing. It tracks dirs scanned, files scanned, total size, and current path. A `force_report()` call flushes immediately on scan completion.

### Error Resilience

Each `os.scandir()` entry is wrapped in try/except. A permission error on one directory doesn't abort the scan -- the error is stored in `FSNode.error` and the scan continues with partial results.

### Symlink Handling

Symlinks are never followed. They are counted as files with their own size (the link itself, not the target). This prevents infinite loops and double-counting.

### Caching

`ScanCache` stores scan results as JSON in `~/.cache/fsmonitor-cli/`. Cache keys are derived from the scanned path. Invalidation is conservative: the root directory's mtime is compared against the cached mtime, and any mismatch invalidates the entire cache.

## Data Model

### FSNode

The core tree structure (`models/tree.py`):

```python
@dataclass(slots=True)
class FSNode:
    name: str              # basename
    path: str              # absolute path
    size: int              # subtree total (files + children)
    own_size: int          # direct file sizes only
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
- `find(path)` -- recursive path lookup
- `size_percent(parent_size)` -- percentage of parent

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

SQLite with WAL mode, stored at `~/.local/share/fsmonitor-cli/data.db` (respects `XDG_DATA_HOME`).

### Schema

**snapshots** -- One row per scan:

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | auto-increment |
| root_path | TEXT NOT NULL | resolved path that was scanned |
| timestamp | TEXT NOT NULL | ISO 8601 |
| total_size | INTEGER | bytes |
| file_count | INTEGER | |
| dir_count | INTEGER | |
| scan_duration | REAL | seconds |
| label | TEXT | optional user label |

**nodes** -- Full tree for each snapshot:

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | |
| snapshot_id | INTEGER FK | references snapshots(id) |
| path | TEXT | absolute path |
| parent_path | TEXT | parent directory |
| name | TEXT | basename |
| size, own_size | INTEGER | bytes |
| file_count, dir_count | INTEGER | |
| is_dir | INTEGER | boolean |
| mtime | REAL | epoch |
| depth | INTEGER | |
| error | TEXT | nullable |

Indexed on `(snapshot_id, path)` and `(snapshot_id, parent_path)`.

**Other tables:** `alert_rules`, `alert_events`, `deletion_log`, `schema_version`.

### Snapshot Queries

`list_snapshots(root_path)` uses ancestor matching: given path `/a/b`, it finds snapshots where `root_path` is `/a/b` or any ancestor (e.g., `/a`). This means exploring a subdirectory in the TUI still surfaces snapshots from a parent `watch`.

`compare_snapshots(old_id, new_id)` does a full outer join on the `nodes` tables of two snapshots, returning `SizeDelta` objects for paths that changed, appeared, or disappeared. A `min_delta` threshold filters out noise.

`get_size_history(path)` joins `nodes` with `snapshots` to return `(timestamp, size)` pairs for a specific path across all snapshots.

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

`delete_targets()` removes directories with `shutil.rmtree()` and files with `os.unlink()`. Each deletion is logged to the `deletion_log` table. A `dry_run` mode is available. Failed deletions are collected and reported without aborting the batch.

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
fsmonitor-cli (no subcommand)
  -> FSMonitorApp(show_welcome=True)
  -> on_mount: push WelcomeScreen
  -> user picks path -> dismiss(path)
  -> _on_welcome_result callback
  -> _launch_explorer: install explorer/cleanup/monitor/settings screens
  -> push explorer screen, scan begins

fsmonitor-cli scan <path>
  -> CLI-only, no TUI
```

### Screen Management

`FSMonitorApp` installs all four screens (explorer, cleanup, monitor, settings) after the welcome screen completes. Screens are installed (not pushed) so they persist when switching between modes with `E`/`C`/`M`.

- `switch_screen()` swaps the current screen at the same stack level
- `push_screen()` adds a screen on top (used for settings overlay)
- Data flows between screens: explorer's scanned root node is passed to cleanup when switching modes

### Monitor Refresh

The monitor screen loads data both on first mount (`on_mount`) and every time it becomes the active screen (`on_screen_resume`). This ensures fresh data from an ongoing `watch` process is always visible.

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
