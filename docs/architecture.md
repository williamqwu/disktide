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
  glyphs.py              Unicode/ASCII glyph selection + the block-element policy
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

- One worker reads its directory whole (below) and streams the entries in
  bounded chunks (256 entries by default).
- Child-directory jobs materialize only as slots open — no queue holds the
  full tree.
- Each chunk installs file nodes and zero-valued directory placeholders.
- A directory that fits one chunk never streams: it comes back whole, and
  `_apply_whole_directory` installs it in a single pass — the worker's node
  replaces the placeholder, its aggregates are taken as summed rather than
  recomputed, and one `_propagate` walk carries the delta to the root.
  Streaming directories keep both passes, because their result really does
  arrive after their entries. On a home-shaped tree that fast path is nearly
  every directory — 88,000 of them at about seven entries each, against a
  256-entry chunk — and it is most of what the scheduler thread's
  per-directory bill was: 31 µs a directory before it, 17 µs after.
- Aggregates update in O(1) deltas; children sort only when a directory
  settles.
- Live roots use generation-based copy-on-write: once published, later
  mutations clone only modified paths, so old frames remain safe to read.
  Those clones are why a frame nobody reads is not free, and why frames are
  paced by the consumer (below).

The main Textual event loop stays on the main thread. Long-running scans
use `@work(thread=True)`. The screen consumes typed events and marshals
them to the UI thread via `app.call_from_thread()`.

The recursive `scanner.walker.scan_directory()` and `ScanEngine().scan(path)`
APIs remain for diagnostics and `tool/` scripts; product code does not
construct the engine directly.

`MonitorService` is written by four threads: the session loop, the event
backend's callback thread, the scan-event consumer, and whichever thread calls
`stop_session` (the TUI's is the UI thread, which uses `wait=False`). All four
persist monitor status the same way -- read the row, edit fields, write it back
-- so `_status_lock`, a plain `RLock`, is held across each of those sequences
end to end, and across `release_monitor_lease`, which writes
`activity_state='no-host'` itself. Anything a run decides *from* service state
belongs inside the same acquisition as the save that records it: `hosted`, for
one, is read from `_held_leases` and `_session_stop` under the lock that the
release also needs.

The lock is never held across `backend.stop()` or a backend start, a scan, a
thread join, `self._condition.wait`, or `_emit`. `backend.stop()` joins the
backend's watcher thread, and that thread may be inside
`_handle_filesystem_event` waiting for the lock, so `_stop_event_backend` pops
and stops the backend before it takes it; `_emit` hands the event to arbitrary
consumer callables, so sites collect what to emit, release, and then emit. The
lock order is `_status_lock` then `_condition`, never the reverse: no
`with self._condition:` block may call into the repository or into a method
that takes `_status_lock`.

### One GIL Release Per Directory

The scanner's remaining distance from `du`, `diskus` and `gdu` was never
syscalls; it was the GIL. `DirEntry.stat` releases the GIL for the `fstatat`
and takes it again afterwards, so a scan of the 88,000-directory fixture
made about 976,000 handoffs — one per entry — and every one of them put the
worker at the back of a queue behind the scheduler thread. With a busy
scheduler each handoff can cost up to the 5 ms switch interval, which is why
**eight workers finished a warm local tree slower than one** (raw 1w 13.2 s,
raw 8w 22.5–24.1 s, of which 17–18 s was system time).

`disktide/scanner/accel.py` picks a `scan_dir(fd, stat_dirs=False)` at
import. It returns one tuple per entry —
`(name, d_type, errno, mode, size, blocks, dev, ino, nlink, mtime)` — for a
whole directory:

- **`_scanfast`**, a ~200-line C extension, does the `fdopendir`, the
  `readdir` loop and an `fstatat(AT_SYMLINK_NOFOLLOW)` for every
  non-directory entry inside a single `Py_BEGIN_ALLOW_THREADS`. One handoff
  per directory instead of one per entry: 88,000 instead of 976,000.
- **`_scanfast_py`** does the same work through `os.scandir(fd)` and
  `DirEntry.stat(follow_symlinks=False)`, one entry at a time, and produces
  the same tuples.

The scheduler has one entry loop, not two, and it consumes tuples. That is
the property the two backends are held to: `tool/dump_tree.py --backend`
and `tests/test_scan_accel.py` compare them node for node, and the trees are
byte-identical. Neither reader owns the caller's descriptor — both `dup` it
before `fdopendir`, exactly as `os.scandir(fd)` does — so the scheduler
still closes the fd it opened, in the `finally` it always had.

Directories are not statted by the read (there is nothing in a directory's
own stat the walk needs at that point); a filesystem that answers
`DT_UNKNOWN` falls through to `S_ISDIR` on the mode, which is the same
branch it always took. On such a filesystem an entry that vanishes between
the readdir and the stat is now counted as vanished, where
`DirEntry.is_file()` used to swallow the `ENOENT` and drop the entry from
the counts entirely. It is a caveat about `d_type`-less filesystems in
general and not about any particular mount -- ext4, xfs and the NFSv4 homes
this was measured on all fill `d_type` in, which is why the byte-identity
dumps against the pre-change build are identical.

Three consequences worth knowing:

- **Chunking is unchanged.** The read is whole-directory; the flush is not.
  A 300,000-entry directory still publishes a checkpoint every 256 entries,
  so a live scan of one updates as it goes.
- **Cancellation is coarser inside a single directory.** It is still checked
  between directories and between entries, but a C directory read is not
  interruptible: about 0.3 s for a 300,000-entry directory, microseconds for
  an ordinary one.
- **One directory's listing is held whole.** `os.scandir` was lazy; a batched
  read is a list of tuples the size of the directory. At seven entries per
  directory — a home-shaped tree — that is nothing, and it is bounded by the
  largest single directory rather than by the tree. On a directory of 300,000
  files in one place it is 37 MB: peak RSS 200 → 237 MB raw, 205 → 244 MB
  live, while the scan itself gets *faster* (1.30 → 1.07 s raw, 1.73 → 1.52 s
  live) — the streaming path is not what the batch costs.

The extension is optional at every level. It is compiled by the build hook
when a compiler is present (see `docs/contributing.md`), absent from a pure
wheel, and switched off by `DISKTIDE_ACCEL=0`; `disktide doctor` prints
which reader is live. What the fallback loses is speed and nothing else.

### The Collector and the Scan

A scan builds about a million objects the cyclic garbage collector tracks —
one `LeafNode` per file, one `FSNode` and one child list per directory — and
every one of them is acyclic. A node points at its children, at strings and
at numbers; `parent_path` is spelled from the path string, `_sorted_cache`
points down, `LiveViewNode.children` is a tuple of children, and the
scheduler's `_DirectoryState` holds a node without the node holding it. So a
full collection during a scan walks the whole tree looking for cycles it
cannot find, and does that again every time the allocation counter crosses
its threshold — nine to twelve times on a tree that size.

Measured on the 88,000-directory fixture at one worker, that is **22% of a
raw scan and 30% of a live one**. Two rounds of profiling missed it, and
could not have found it: a collection runs inside whichever allocation
triggered it, so py-spy charges its time to `_scan_open_directory`,
`make_file_node` and `_apply_result`. `gc.callbacks` is what sees it, which
is why `tool/bench_scan.py --json` now reports a `gc` object.

`scanner/gcpause.py` has two halves, and they answer different questions:

- **`collector_paused()`** covers the walk. Counted and thread-safe, so
  `ScanEngine.scan` and `ScanService._execute_active` can both take it; it
  restores whatever state it found, including leaving the collector off for
  an embedder that had turned it off. It is process-wide — there is no
  per-thread collector — so cyclic garbage that a live UI makes during a scan
  is deferred until the scan ends.
- **`freeze_retained_tree()`** covers what is left. A finished tree is
  usually the tree the process keeps, so every full collection after it walks
  a million objects again, this time as a pause on whichever thread Python
  was running — 720-770 ms with the fixture's tree resident, and 0.0 ms once
  it is frozen. Called from `ScanEngine.scan` inside the pause, and gated at
  `FREEZE_MIN_ENTRIES`, below which freezing buys nothing and a suite of
  twenty-node fixtures would freeze its heap hundreds of times. **The first
  freeze in a process collects nothing**: there is nothing pinned to hand
  back and the walk's own objects are acyclic, so that collection could only
  walk a million nodes and find none — 0.7 to 1.3 s that `disktide scan` paid
  on its way out and got nothing for. Every freeze after it unfreezes first
  and then must collect, because what the last one pinned is now partly dead.
- **`recollect_retained()`** runs the same three steps from whoever *owns*
  the tree rather than from the walk that built it. Nothing in the app calls
  it: it is kept, and tested, for an embedder that holds trees across scans
  and has no equivalent of the releases below. The explorer used to arm it a
  second after every completion, and that cost 2.2-2.8 s of stop-the-world on
  the 88,000-directory fixture — a freeze right after a scan, which is
  exactly when a user is looking at the screen.

**The releases are what replaced it, and they are the better answer**: a
refcount that reaches zero costs nothing, where a collection that finds the
same garbage has to walk a million nodes first. Three references to a
replaced tree outlive it, and each one is now dropped by assignment at the
moment it goes stale:

- **Every materialised row.** `Tree.clear()` does not delete rows, it stops
  referring to them, and a `TreeNode` graph is cyclic — so the old rows
  survive as an island still holding the `FSNode`s they were drawn from.
  `SizeTree._release_row_data` walks them before `clear()`; `_unregister_subtree`
  does the same for rows removed during a live update.
- **`ScanRun.root`**, a second reference to a tree the screen already holds.
  It outlives the run by more than it looks: `_run_scan` is a
  `@work(thread=True)` and Textual keeps a worker's arguments for the life of
  the worker record. `ExplorerScreen._release_retiring_run` nulls it once the
  replacement tree is installed.
- **The category rollup's worker argument**, for the same reason — a root
  passed positionally to `@work` stays referenced by the `functools.partial`
  Textual holds. It goes through `_category_index_source` instead.

Measured in the real TUI, three rescans of the fixture: **1069, 1123, 1165 MB
resident against a base at 1261, 1273, 1263** — the branch settles 8 % *below*
the revision that never froze anything, with no collection pause at all.

The tree being acyclic is necessary and **not sufficient**, and the
difference is the whole reason for the unfreeze and the recollect.
Refcounting frees a frozen tree when the next scan replaces it, which is what
`tests/test_gc_floor.py` pins for all four node types. But what matters is
whether anything *holding* the tree is cyclic, and behind the explorer it is:
a Textual widget graph is full of cycles and keeps node data on the rows it
has materialised. Pinned, those widgets stopped being collected once they
became garbage, and each held a whole tree — three rescans of the fixture in
the real TUI went 654 MB, 1251, 1847, 2440 and climbing, against a flat
1250-1260 on the base. Handing the last freeze back at the next one bounded
that; releasing the three references above, so the tree's refcount reaches
zero without any collection at all, is what removed it.

One trap that cost a whole round of measurements. The embedder check —
"has anything been frozen that we did not freeze" — was asked as
`gc.get_freeze_count() != 0`, and that is **not** zero on every interpreter:
the one `uv tool install` builds starts with 375 objects in the permanent
generation where a plain venv starts with none. So the freeze quietly turned
itself off on the interpreter the app and every benchmark run on, and stayed
on in the venv the tests run in. Nothing failed and no test caught it; the
numbers were simply measuring the pause alone. It is asked against
`_BASELINE_FROZEN`, captured at import, and `tests/test_gc_floor.py` pins
that a non-empty starting permanent generation still freezes.

`tests/test_gc_floor.py` also pins that the collector comes back on from
every exit a scan has, and that each freeze hands the last one back. If a
node ever grows a back-reference, that test fails before the leak reaches
`watch`, which scans the same tree for days.

One thing to know when measuring: `gc.get_objects()` does **not** report the
permanent generation, so after a freeze it reads as an almost empty heap.

**Known issue: repeated raw scans in one process drift upward, and the gate
that watches for it is noisy.** `tool/soak_memory.py --mode raw` on the
88,000-directory fixture grew from 65.6 MB at iteration 3 to 103.3 MB at
iteration 25 -- +57.5% against its own 5% budget, so it exits 1. The drift
is pymalloc keeping nearly empty arenas, not scan data being retained.
Three measurements on the same fixture (raw, one worker, 25 scans in one
process) pin that down:

- `sys._debugmallocstats()` after every scan. Bytes in allocated blocks --
  the Python objects actually alive between scans -- stay at 5.4-5.5 MB
  from the first scan to the last. Arenas currently allocated go from 34 at
  scan 3 to 47-50 at scan 25, and RSS goes from 42 to 55-57 MB over the
  same scans. An arena is 1 MiB; the RSS curve is the arena curve.
- `tracemalloc` between scan 3 and scan 12: +0.03 MB in total, a few
  hundred small blocks -- a rehashed `threading._active` table, the strings
  the cgroup and mount probes read, a weak-set entry. A scan allocates and
  frees about 550 arenas, and pymalloc returns an arena to the OS only when
  every pool in it is empty, so each of those survivors pins the whole
  arena it landed in. About one arena in two outlives its scan.
- `PYTHONMALLOC=malloc`, so glibc serves every allocation instead of
  pymalloc: 636-637 MB after every one of the 25 scans. glibc never hands
  the freed tree back at all, and never drifts either.

So the process accumulates near-empty 1 MiB arenas at roughly half a
megabyte a scan, which is why the same build scored +4.9% over 14
iterations and +57.5% over 25, and why the 5% gate read at the default 20
iterations can pass and fail on the same build. It is why the earlier
candidates all came up empty: the pure-Python fallback grows more (+84.8%)
because it allocates more; `MALLOC_ARENA_MAX=2` changes nothing because
pymalloc's arenas are not glibc's; a single worker still drifts (+40.5%)
because the survivors are not per-thread. A 20,000-directory tree shows no
drift at all over 12 scans (arenas 30-34 throughout), and `--mode live`,
which is the mode the explorer takes, is within budget: 100.1 MB to
102.2 MB over 12 iterations, +2.1%.

The freeze is not the cause; it is what keeps the number small. With
`FREEZE_MIN_ENTRIES` raised so `freeze_retained_tree()` never runs, RSS is
flatter and four and a half times higher: iteration 3 at 332.0 MB to
iteration 14 at 333.7 MB, against 71.7 MB to 75.2 MB with the freeze on.
The `gc.get_freeze_count()` that grows by exactly one per scan is the soak
tool's own per-iteration sample dict, not anything in the scanner.

Read the raw-mode soak as a measurement, not as a gate. Closing it means
making a walk leave nothing behind in its arenas: find the few kilobytes
that survive a scan, starting with the platform probes in
`collectors/platform/linux.py` that `select_scan_workers()` runs on every
engine, and allocate or cache them outside the walk.

### Adaptive Worker Count

`sysinfo.select_scan_workers()` resolves policy against the actual scan
path. Automatic selection considers CPU availability, load, memory,
filesystem/media classification, and a bounded 64-entry/75ms metadata
sample:

| Condition | Workers |
|-----------|---------|
| Low-latency local or RAM-backed storage | 1 |
| Rotational local storage | ≤ 2 |
| Network or FUSE mount, unsampled | 8 (measured network base) |
| Latency-bound mount, ≥ 0.5 / ≥ 1 / ≥ 3 ms per entry | 16 / 32 / 64 |
| Measured high-latency *local* storage | ≤ 4 |
| Shared host with no allocation and other users present | ≤ 2 |
| Host load > 0.75 × CPUs (not on an allocated slice) | halved |
| < 512 MB available memory | 1 |
| Probe error | 1 (conservative fallback) |

An explicit `workers` value skips detection but not the ceiling.
`worker_ceiling(available_cpus)` is `max(64, 4 × available_cpus)`: four
workers per visible CPU because a worker is asleep in a `stat` for most of
its life, and a floor at 64 so the widest measured latency tier stays
reachable on a two-core box. A request above it is *clamped*, never refused
--- `effective_workers` becomes the ceiling, `mode` stays `explicit`, and the
reason says so. `-w 0` and negative values still raise `ValueError` and exit
2.

Clamping and three other host facts arrive as `ScanWorkerSelection.warnings`,
a tuple of one-sentence strings: an explicit count above the ceiling, more
than `_SHARED_HOST_WORKER_CAP` workers on a shared host, a request above the
recommendation while host load is already over 0.75 × CPUs (skipped on an
allocated slice for the same reason the auto policy skips its load guard
there), and more workers than CPUs on a mount that is not latency-bound. They are advisory --- the
request is honoured up to the ceiling either way. `disktide scan` prints them
as `Warning:` lines on stderr (in `--json` mode too, so the payload stays
clean) and carries them in `workers.warnings`; the explorer raises one
notification each from its `ScanStarted` handler.

### Progress Reporting

`ScanProgressOverlay` arms one `set_timer` per scan in `start()`
(`HINT_AFTER_SECONDS`, 20 s). If the scan is still running when it fires, a
`Static` composed hidden becomes a two- or three-line hint naming the worker
count and the `,` → Workers → `r` route to changing it; a third line appears
when `ScanWorkerSelection.warnings` reports a shared host or a clamp. It is
one shot and never re-armed, so the overlay's only recurring timer remains
the 5 Hz repaint pacing; `start()` and every terminal state
(`_stop_repainting`) cancel it and take the block off screen.

`ProgressThrottle` batches callbacks to 100ms intervals.
`ScanProgressSnapshot` captures completed/queued directories, queue depth,
active workers, files, bytes, current path, and elapsed time. Progress
reports Logical bytes because Unique requires global hardlink reconciliation;
the final tree additionally carries Allocated and Unique.

That reconciliation only has something to decide when two paths share an
inode. `_recalculate_directory` already touches every child of every
directory, so it answers "was any direct entry a hardlinked leaf" on the way
past, and the scheduler carries the answer out on `ScheduledTree`. When it is
no — which is every tree that is not a package cache —
`accounting.mirror_allocated_as_unique` writes the answer instead of deriving
it, because with no inode shared a node's unique allocated size *is* its
allocated size, None propagation included. On the 88,000-directory fixture
that is 114 ms against 368 ms, on the tail every caller waits through with an
empty queue and no workers.

### Measurement and Scope Policy

- **Logical**: `st_size` payload aggregate over files and symlinks; a
  directory's own `st_size` is not payload and is not added.
- **Allocated**: `st_blocks × 512` per visible file, symlink *and directory*
  path, including the node itself — the number `du` reports. A directory's
  own blocks come from the `fstat` of the descriptor the scan opened it on.
- **Unique**: one deterministic lexical owner per `(st_dev, st_ino)`. Only
  leaves can share an inode, so directory blocks survive the dedup;
  `accounting.finalize_unique_allocated` recovers them as
  `own_allocated_size - sum(leaf.own_allocated_size)` rather than re-stat'ing.
- Missing `st_blocks` → unavailable, never zero.
- One-filesystem mode stops at device boundaries, keeping an `xdev` node.
- Descendant pseudo mounts excluded by default; explicit pseudo root allowed.
- Max-depth and policy exclusions are separate from access errors.

See ADR 0001 for the storage metric contract.

### Error Resilience

Each entry carries its own errno from the directory read, and the scheduler
sorts it into *vanished* (`ENOENT`, `ESTALE`, `ENOTDIR` — the tree moved) or
*inaccessible* (everything else). A permission error on a directory stores
the error in `FSNode.error` and the scan continues with partial results. A
`readdir` that fails part way through a directory comes back as one final
entry carrying that errno, so everything read before it is kept and the
failure is counted once.

Coverage gaps are counted, never subtracted from the metric's *kind*. A
denied directory contributes its own blocks and nothing under it; a
depth-limited one the same; vanished, excluded and self-ancestor directories
contribute zero. `allocated_size` and `unique_allocated_size` are `None` only
where the platform has no `st_blocks`, so one unreadable directory cannot
blank the scan root's totals — the invariant is checked on every applied
result (`scheduler_invariants` I8).

### Symlink Handling

Symlinks are never recursed into — stored as leaf nodes sized by the link
itself (`lstat`). Target classification (readlink + follow stat) is
**deferred**: the scan pays only the one `lstat` the directory read already
made, and `classify_symlink` runs on demand (Details panel, `i`
navigation). The result is cached via `link_classified`.

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

The backup is written to `data.db.pre-vN.bak.partial` and renamed only when
it is complete, so an interrupted run leaves nothing that could be mistaken
for a recovery point; an existing backup is validated before it is reused.
The copy runs in 4096-page steps and reports its progress, which the CLI
prints on stderr when stderr is a terminal.

There is no whole-file integrity check on the open path. `PRAGMA
quick_check` reads every page, and it used to run on every
`Database._open()`; `doctor` performs it as a named step instead, skipping it
above 256 MiB unless `--check-integrity` is given. What the open path keeps
is a `sqlite_master` read, which costs the schema rather than the data and is
what makes the read-only recovery refuse a file that is not a database.

The version is read twice: once cheaply, to skip the lock entirely for the
usual already-migrated case, and again inside the `BEGIN IMMEDIATE` that
guards the DDL. The second read is what makes two processes creating the
same database at once safe -- the one that gets the lock second finds the
work already done and commits without replaying anything. The one statement
`busy_timeout` does not cover, `PRAGMA journal_mode=WAL`, is retried for the
same five seconds by hand.

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
  0.25s **and never faster than the consumer applies them**. Each update
  carries the COW root, changed nodes, a bounded immutable `LiveViewNode`
  model, and an `ack`.
- **Frames are paced by the consumer, not by a clock.** Every publish bumps
  the generation, and the next write into any directory chain then clones
  that chain's spine so the frame that went out stays immutable — so a frame
  nobody looks at is paid for twice, once to build and once in the clones it
  forces on the walk behind it. A consumer calls `ScanTreeUpdate.ack` when it
  has *applied* a frame; until then the scheduler skips non-forced publishes,
  keeping `changed_nodes` and `stable_paths` so the next frame carries the
  union rather than losing the difference. Forced publishes (depth-0
  completions, entry-chunk checkpoints) and the one frame of credit each
  leaves behind are never held back, so a scan that finishes inside one
  window still shows something before it shows everything. A consumer that
  never acks — the events-only service path, a headless bench, a future web
  UI — is fed on a cap of eight callback intervals, counted in intervals so
  that `tree_callback_interval=0.0` still means "every frame". On the
  88,000-directory fixture at one worker this halves the frames built, 53 to
  29; `ExplorerScreen` acks from `_apply_live_ui`, behind the duty gate.
- `SizeTree.apply_live_update()` updates materialized nodes in place; cursor
  and expanded state survive. The final tree reloads after deterministic
  accounting completes.
- Treemap and Sunburst consume the immutable model, limited to depth 2 and
  96 children per node. Excess children collapse into an aggregate "Other."
- Late updates from an older scan cannot replace a newer run.
- `live_scan_render = "auto"` (default) requires ≥ 80×24; otherwise
  progress-only mode. It is a legibility check, not a capacity one.

**The UI thread is on a budget, and live snapshots are what it spends
it on.** The service coalesces `NodeAggregateUpdated`, so the explorer is
handed a frame exactly as fast as it can draw one, and every millisecond
it spends drawing is a millisecond the walk does not get: the GIL makes a
UI-thread budget a scan budget. A 982k-entry local tree scanned in 21.0 s
with the chart off and 152.3 s with it on; a 700k-entry home over NFS in
36.8 s against ~420 s.

`ExplorerScreen._maybe_apply_live_ui` is a closed loop, not an estimate.
At each applied snapshot the screen stamps `thread_time()` and
`monotonic()`; the next one waits until the CPU spent since then is back
under one part in `_LIVE_UI_DUTY` (8) of the wall clock that passed,
floored at the scheduler's own 0.25 s publish interval. Nothing has to be
predicted and nothing can be missed — a chart that grows from 500 arcs to
9,000 over one scan, a geometry table built for a new widget size, a
compositor pass that lands three callbacks later are all inside the
window, because the window is "since last time". The two measurements
that were tried first are in the comment above the constant, with the
numbers that ruled them out.

One gate covers the tree panel, the chart and the compositor pass they
queue. The progress overlay is not inside it — it is one string and three
numbers and the newest of them is wanted whether or not anything is about
to be drawn — but it is paced on its own, because "cheap per event" was
true per call and wrong per scan. `ScanProgressUpdated` arrives at up to
20 Hz, each one dirtied three widgets, and Textual's indeterminate `Bar`
armed `auto_refresh = 1/15` to animate a band that `animation_level =
"none"` renders identically every frame. Together they were 1.15 s of
UI-thread CPU over a 15.2 s scan of the 88k fixture at 307x69 with the
chart off entirely. `ScanProgressOverlay` now keeps the newest report and
redraws at 5 Hz, and clears the bar's timer: 0.42 s. A skipped frame arms
one `set_timer` for what is still
owed and is replaced by the next, so a scan that goes quiet still lands
its last picture; the timer goes back through the gate rather than past
it. Completion always paints. `tool/bench_scan.py --mode live --paint
COLSxROWS` runs the same loop on the service's dispatch thread without a
terminal (39.4 s no paint / 39.6 s paced / 62.4 s per frame / 256.9 s per
frame with the geometry cache off, on the same 88k-directory tree).

Only the newest frame's `changed_nodes` is applied, not the union of the
skipped ones: `TreeScanScheduler._record_changed` walks a settled
directory's whole ancestor chain, so every row the tree has materialised
is in every publish that touches anything below it. Accumulating instead
put 45,000 nodes into one `apply_live_update`, which sorted them all
before discarding everything it could not show. `changed_nodes` now ships
in record order — nothing consumed the (depth, path) order the scheduler
used to build — and `SizeTree.apply_live_update` filters to the rows it
has actually materialised before it sorts, which is dozens rather than
thousands.

What is left is the rest of the live pipeline, not the UI. The 307x69 A/B
is ~21.7 s off against 29-38 s on, and pinning the gate shut so the
snapshot work never happens still costs 28.0 s. That floor is the
copy-on-write publishing itself — `emit_tree_updates` roughly doubles the
process's CPU whether or not anything is drawn — and it is on the
scheduler's own thread, where nothing the UI does can reach it.

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

### Web-Shell Glyph Set

A browser terminal (xterm.js: Open OnDemand, JupyterLab) defaults to
`courier-new, courier, monospace`, and Courier New decides this policy. It has
no glyph for the eighth blocks or the quadrants, so the browser falls back to a
proportional face and draws them at *that* font's advance — a border row built
from those comes out 1.2–1.8× too wide. It *does* carry the eight block
elements WGL4 defines (`▀ ▄ █ ▌ ▐ ░ ▒ ▓`), and 04ee56b concluded from that they
were safe; they are not. Its ink for them is not fitted to a terminal cell:
measured on an explorer capture at 307×71, `░` is ~1.1 cells wide and ~1.4 rows
tall (one row's bar bleeding over the size text of its neighbours) and `▀`/`▄`
are narrower than the cell (a comb of slits along every horizontal edge, and
the rows carrying a long run of them shifted by up to 0.6 cell).

So the rule is about block elements as a class:

> Nothing DiskTide draws itself may be a block element (U+2580–U+259F). A fill
> is a background colour on spaces. A ramp that needs height is ASCII.

Box drawing (U+2500–U+257F) including the heavy forms, `▶▼■●○◐`, the arrows and
text were verified glyph by glyph on the same capture and stay.

`disktide/glyphs.py` is the single source: `UNSAFE_GLYPHS` (the whole of
U+2580–U+259F), `WEB_SAFE_GLYPHS` (the reviewed allowlist), `SAFE_BORDER_STYLES`
/ `UNSAFE_BORDER_STYLES` (a partition of Textual's `BORDER_CHARS`; `thick` and
`block` are on the unsafe side now, and `double` is the heaviest box left), the
replacement `ScrollBarRender` bar lists and the replacement `ToggleButton`
sides. It imports nothing, including Textual.

Three places consume it. `DiskTideApp.CSS` restates every Textual border that
would resolve to an unsafe style — `tall` on Input/Button/ToggleButton/Switch/
Select, `hkey` on Collapsible and the command palette, `vkey` on the footer and
the key panels, `outer` on toasts — at the same geometry in `solid` and
`blank`; app-level CSS outranks every `DEFAULT_CSS` rule including `!important`
ones, so one rule per widget covers all of its states. The app's own panels and
modals ask for `double` where they used to ask for `thick`.
`use_web_safe_scrollbars()` and `use_web_safe_toggle_buttons()` run from
`DiskTideApp.__init__`, before the first screen, because both replace class
attributes read at render time. And every fill the app draws by hand is a
background: `SizeTree._append_share` (with a `render_label` override that
re-applies the bar's background *after* the cursor style, which would otherwise
win), `fs_overview._usage_bar` and `_build_summary`, `settings.theme_preview`,
and `scan_progress.FilledBar`, a `Bar` renderable whose three glyphs are spaces
and whose styles are turned into backgrounds. The `bar_track` ink role is the
themed colour behind a bar. `trend_chart._update_plot` passes `marker="dot"` to
plotext, whose default is its high-density set (`▀▄▖▗▘▚▝▞`); `•` is on the
allowlist for that one caller, and plotext's axes and frame are box drawing
already.

The `tiles` ring shape belongs to the same story: its vertical quantum is a
whole row (not the framebuffer's half-row), its centre snaps to a row boundary,
and its subsamples are all taken at the cell centre, so `_render_cells` emits
nothing but spaces for it. `disc` and `fill` keep the half-block pass because
they are round.

`tests/test_web_glyphs.py` gates it host-independently (resolved border styles
across every screen, both colour modes, a rendered-thumb sweep, and the scan
overlay's bar); `tool/capture_glyphs.py` checks real panes under tmux at 307×71
and 120×32, walking every screen plus a running scan (`--slow-tree`).
`ui.safe_rendering` is a separate, orthogonal switch: the mode for a terminal
that has no background colours to spend either.

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
distributions, ≤ 20 MiB, no third-party native extension — the scanner's own
optional `_scanfast` is exempt, because nothing requires it.
