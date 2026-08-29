# Changelog

## v0.2.24

**Category colors that survive theme and colorblindness, dominance-tinted directories, and the return of the mouse**

- **one palette, six categories** The old scheme gave eleven file categories per-theme hues, and the warm theme squeezed all eleven into an ~80° slice of the hue wheel: measured at the rendered colors, its worst pair (data vs build) sat at ΔE 1.9 -- indistinguishable to *everyone*, with 26 of 55 pairs below the normal-vision floor. Eleven hues cannot be made pairwise distinguishable; six can. Categories that call for the same cleanup decision were merged (documents with configuration, datasets with model checkpoints, images with audio/video, build output with logs), and the six hues were grid-searched in OKLCH against a colorblind-simulation validator over *all* pairs -- any two categories can be neighbours in a ring -- landing at worst-pair ΔE 8.0 under protan/deutan simulation and 17.0 under normal vision, zero pairs below floor. `tool/gen_palette.py` regenerates the tables from the OKLCH anchors. Themes now vary the *neutrals'* temperature (warm gray vs cold gray) and vivid's chroma, never the data encoding, so a category keeps its color in every theme; `mono` stays achromatic. The centre label also composites on the widget's measured background now instead of a hardcoded gray.
- **directories carry the story** A sunburst's inner rings are almost all directories, and directories rendered as one flat neutral -- the chart's dominant area said nothing. After a scan completes, a background pass rolls every directory up into a per-category byte histogram (13.6 ms for 25k nodes, off the UI thread), and a directory whose bytes are >=50% one category is tinted towards that category's hue with chroma scaled by the share, so a wedge that is "all checkpoints" looks like it from ring 1 outward. Below a majority the tint would lie, so the directory stays neutral; live scans stay neutral too, since partial totals would crown the wrong winner. The legend now reports each category's share of the scanned bytes (`■ data 46%`), largest first. Versioned shared libraries (`libcudnn.so.9`) peel their numeric suffixes and classify as the build output they are instead of falling to "other".
- **mouse support is back** It left in March because hover handlers rebuilt the info panel at 60+ events/sec and every viz tab updated at once; the commit that removed it also fixed those pathologies, and a handlerless MouseMove measures 0.30 ms today. What keeps it cheap is structural: hover only hit-tests -- the treemap already had an O(1) cell grid, and the sunburst now keeps its per-ring bisect tables on the layout -- and sets a tooltip; only a click may trigger a recompute, which costs what a keypress costs. Click a tree row, an arc, or a rect to navigate; every path reuses the keyboard flow with all its guards, and clicking the sunburst's centre goes up exactly one drill level and never launches a rescan. The tree cursor also brightens its arc on the chart, pushed behind a 0.12 s settle so holding an arrow key doesn't pay ~40 ms per step. `ui.mouse = false` or `--no-mouse` opts out; terminal-native text selection wants Shift+drag while mouse reporting is on. Restoring click-to-select surfaced two latent `SizeTree.select_path` bugs -- file targets never expanded their parents, and the cursor line was read before the line cache rebuilt -- both fixed for the keyboard restore path too.
- **the interaction loop stopped paying rent** Four measured stalls. Textual's tab-switch animation was ~320 ms of a ~420 ms switch and a burst of frames down the SSH pipe, so animations default off (`TEXTUAL_ANIMATIONS` still wins if set). The info panel rebuilt its Rich table on every cursor move even while the Details tab was hidden; it renders only when visible now, and activating the tab replays the cursor's node instead of silently showing the drill root. Highlighting a symlink ran readlink+stat on the UI thread -- two server round-trips on a cold NFS attribute cache -- and now resolves off-thread behind a "Resolving…" row. Diff mode recomputed the whole chart per keystroke and now shares the same settle timer, and `Ctrl+D`/`Ctrl+U` move the cursor once instead of looping line-by-line through the highlight pipeline.

## v0.2.23

**A welcome screen that no longer pays for the whole application**

- **deferred startup imports** `app.py` imported the four services and the five mode screens at module scope, so launching the TUI built the entire application graph before drawing a screen that uses none of it. The services are now lazy properties on `DiskTideApp` and the screens are imported inside `_launch_explorer()`, which takes `import disktide.app` from 563 modules and 486 module-file opens to 406 and 333 -- 99 `disktide` modules down to 27. `textual-plotext` and `plotext` are the largest thing to go: they arrive through the monitor screen's trend chart and are not needed until that screen exists.
- **why that matters on shared storage** Reaching the welcome screen is almost pure import cost, and on a network filesystem that cost *is* the startup time. On the NFSv4 cluster home used for benchmarking, a module file measured ~16 ms to fault in cold against ~0.4 ms once the page cache held it, so every module on the pre-welcome path is worth roughly 40x its size on a node's first launch. The 153 files dropped from that path are about 2.5 s off a cold launch; warm process time went from ~0.52 s to ~0.37 s.
- **teardown no longer builds what it tears down** `_perform_quit` called `stop_session()` and `cancel_all()` unconditionally, which through the new properties would have constructed both services -- and paid for their imports -- purely to shut them down again. It now reads the backing fields and skips whatever was never built, so quitting from the welcome screen costs nothing.
- **the boundary is asserted** Nothing in the code enforces which modules may load at startup, and one convenience import at module scope silently undoes it. `tests/test_startup_imports.py` reads `sys.modules` from a subprocess -- the suite has long since imported everything by the time any test runs -- and fails if a service, a mode screen, the plot backend, or the cleanup rule loader is resident after `import disktide.app`, with a budget on the resident `disktide` module count.

## v0.2.22

**A sunburst that is actually a circle, painted with anti-aliased half-blocks**

- **cell aspect** Every chart assumed a character cell was exactly twice as tall as it is wide, because that is the only ratio at which a braille dot is square. Real cells are not: a 14px monospace face at 1.2 line height gives 7x17 pixels, a ratio of 2.43, and drawing a circle as if it were 2.0 stretched the disc vertically by 21%. The sunburst was a visible ellipse. Most terminals report the pane's pixel size next to its cell size in `TIOCGWINSZ` -- tmux forwards it to every pty it owns -- so the ratio is now measured rather than assumed, and the disc comes out round on any font. `DISKTIDE_CELL_ASPECT=2.43` overrides the measurement; a terminal that reports no pixel size still gets the old 2.0.
- **half-block renderer** The braille layer is gone. A braille dot is on or off, so a partially covered cell could only be stippled, and against the solid interiors the dotted rim -- and much worse, the dotted walls of every empty wedge, which cut clean through the disc wherever a leaf has no children -- read as dirty plumes rather than edges. The disc is now supersampled into a framebuffer of half-cells, four subsamples each, and every cell resolves to a space over one color or to `▀`/`▄` over two. Rims, wedge walls, and separators all come out as coverage blends. A half-covered cell draws its block as *foreground only*, so the widget's own background shows through and no background estimate can halo the disc. Uniform samples average back to themselves exactly, so ring interiors stay perfectly flat. The whole compute-and-render pass came out about 1.4x faster than the braille path at every pane size.
- **separators** Sibling directories at one depth were given the identical color -- hue and saturation from the scheme, luminance from depth alone -- with nothing drawn between arcs or between rings, so a run of them merged into one undivided blob. Each ring now carries a hairline of its own darkened color just inside its outer edge, and each boundary between siblings carries one along its length, skipped wherever a neighbour is too narrow to survive it. Both are narrower than the subsample spacing, so each subsample mixes by the fraction of its footprint the seam covers; that conserves the seam's ink and resolves it as an even line instead of a dashed one. Sibling directories also alternate slightly in luminance, which carries the division through the arcs too small for a seam.
- **treemap squareness** `compute_layout` squarified in doubled-height space with the factor 2 hard-coded, which is the same wrong assumption in the other chart. It now takes the measured cell aspect, so a block that squarify believes is square is square on the viewer's screen. Callers that pass nothing still get 2.0.
- **drawille** Dropped from the dependencies along with `viz/braille.py`; nothing imports it any more.

## v0.2.21

**Legible visualizations: a solid sunburst with a braille rim, a treemap without slivers**

- **sunburst fill** The ring chart drew every interior cell as a fully-lit braille glyph over a darkened copy of the arc color. Terminal fonts leave gaps between braille dots and between text lines, so that background bled through everywhere and the disc read as a halftone dot grid with horizontal banding -- the "noise" in the chart was the medium, not missing data. A cell whose whole 2x4 dot block belongs to an arc is now painted as a solid block of the arc's own color, undarkened.
- **sunburst rim** Filling an arc set a full-cell background for every dot the stroke touched, so a cell the circle merely clipped still got a whole dark chip and the silhouette was quantized to character cells. Partially covered cells now render the braille glyph alone, with no background, and the edge resolves at 2x4 sub-cell steps against the terminal's own background instead of a staircase halo.
- **sunburst rasterizer** Arcs were filled by stroking concentric integer-radius circles and truncating each sample to a pixel, which wrote some dots many times over and never reached others. The disc is now scan-converted once: a pixel's radius picks the ring and its angle picks the arc by binary search over that ring's intervals. Every covered dot is set exactly once, and the whole compute-and-render pass came out about 1.7x faster at every pane size.
- **sunburst cracks** Arcs spanning under half a degree were discarded *before* being recorded, so nothing owned their slice of the ring and the fill left a hairline of unpainted dots running through it. Such an arc is now emitted -- it simply stops there instead of subdividing further -- and rasterizes as its own hairline of color.
- **treemap aspect** The layout squarified in raw cell units, but a terminal cell is about twice as tall as it is wide, so it was optimizing squareness the viewer never sees; with one dominant sibling the remainder landed in a thin full-height strip. Squarify now runs in doubled-height space and the result is mapped back to cells, so blocks are square on screen and crumb strips fall out as horizontal rows a label fits in.
- **treemap slivers** Nothing consolidated entries too small to draw: `bounded_children` aggregates only past a *count* cap, so a dozen sub-cell siblings each still demanded a rect that the integer snap inflated to the one-cell minimum -- several onto the same cell, where whichever was drawn last erased the rest. Runs of siblings that cannot render are folded into a single labeled "… N more" block, before layout when they are individually sub-cell and after it for strips too thin to carry a label. A child on the selected path is never folded away.
- **treemap overlaps** Inflating a zero-size rect to the minimum moved a boundary its neighbor did not know about; both claimed the cell, and when the neighbor was a *parent* rect an entire subtree was repainted out of existence. The inflated rect now takes the cell from its neighbor rather than drawing on top of it, and endpoints are snapped in coordinates local to the parent so a boundary two rects reach by different arithmetic still lands on one integer.
- **anti-fragmentation invariant** The pattern suite now asserts that every leaf rect owns essentially every cell it covers -- losing at most a single row or column to a neighbor's snap -- and that no rect with area is rendered nowhere. The suite's old demand that at least ten of a hundred sub-cell files each get their own rect was the requirement that produced the overlaps; it now asserts that every child is represented, individually or by an aggregate that accounts for it.
- **area still means size** Nothing small was inflated to make it visible: rectangle area and arc angle continue to encode the selected metric exactly, per ADR 0006. Consolidation changes only what carries the label -- an aggregate block reports the combined size of everything it stands for.

## v0.2.20

**CLI conventions: silent quit, stream discipline, discoverable subcommands**

- **silent quit** Quitting the TUI no longer prints `Exiting...` or `DiskTide closed. Goodbye!`. Both dated from the era when teardown took tens of seconds and the user needed telling that the process was still working; since the `os._exit(0)` shutdown landed in v0.1.6 the whole exit is ~200 ms, so the messages were noise scrolled into the user's shell history. A terminal program that exits successfully says nothing -- `htop`, `ncdu`, `less`, and `vim` all return you to the prompt in silence. The only bytes written after the TUI tears down are now the terminal-restore escapes.
- **scan streams** `disktide scan` wrote its progress counter to stdout without checking for a terminal, so `disktide scan / > report.txt` captured `\r`-separated redraw frames inside the report. Run narration -- queue position, start banner, phase changes, worker selection -- now goes to stderr with the rest of the status, stdout carries the report alone, and the carriage-returned counter is drawn only when stderr is a terminal.
- **scan --json** Added machine-readable scan output, matching `doctor`, `cleanup`, `monitor`, and `alerts`. The document is versioned and carries the run id, status, policy, worker selection, all three byte totals, coverage counts, and the root's direct child directories sorted by the selected metric. A cancelled or failed run still emits a parseable document alongside its non-zero exit code.
- **cleanup --json** `disktide cleanup PATH --json` prefixed its document with `Scanning ... for cleanup targets...` on stdout, and answered `No cleanup targets found.` in plain text, so neither case parsed. The notice moved to stderr and the empty case now returns a document.
- **cleanup subcommands** `cleanup` was one flat command that hand-dispatched `rules`, `history`, `undo`, `purge`, and `quarantine` out of an `UNPROCESSED` argument tuple, so `disktide cleanup rules --help` printed the parent help and no subcommand appeared in any help output. It is now a real command group -- like `monitor` and `alerts` already were -- with per-subcommand help and options. `disktide cleanup PATH` still routes to the default `plan` subcommand, and flag options written before a subcommand (`cleanup --json history`) are hoisted along with it. An option that takes a value has to follow its subcommand (`cleanup history --by category`): the router cannot know an option's arity before it has picked the command, so it stops at the first bare token rather than risk routing an option's value as a subcommand name.
- **watch exit code** Ctrl-C out of `disktide watch` printed to stdout and exited `0`, so a caller could not distinguish an interrupt from a host that reached `--max-time`. It now reports on stderr and exits `130`, matching what `scan` already did.
- **TUI without a terminal** `disktide` with stdin or stdout redirected sat forever waiting on a keypress that a pipe can never deliver. It now fails immediately with an explicit message and exit `1`, pointing at the subcommands for non-interactive use.
- **help text** Three docstrings doing double duty as Sphinx source rendered their ``literal`` markup verbatim in `--help`; the top-level docstring lost its line breaks to Click's paragraph rewrapping. Both are fixed.

## v0.2.19

**Live frame integrity and scheduler invariant coverage**

- **published frames stay frozen** A subdirectory placeholder can go out in a live frame long before the bounded source queue dispatches it; its state inherited the parent's copy-on-write generation, which skipped the clone and let the first entry chunk edit an already-rendered frame in place. Dispatch now stamps the child as stale so it is copied before it is filled in.
- **scheduler invariants are asserted** The suite now validates the scheduler's positional structure on every state change, so a stale cursor or a mis-addressed parent index fails where it happens instead of surfacing later as an aborted scan.
- **shapes that reach the hazards** Added fixtures that put a directory's entries in an order the settle-time sort reshuffles while its child cursor is still open, plus a check that the invariants themselves still fail on the historical defect.
- **randomized soak** Added `tool/soak_scan.py`, which generates adversarial trees and cross-checks scheduler invariants, live-frame immutability, and totals against an independent walker.

## v0.2.18

**Scan scheduler cursor correctness**

- **duplicate directory jobs** Settling a directory now retires its child cursor before the entries are sorted, so the reordering can no longer walk the cursor onto an already-scanned subdirectory and dispatch it a second time.
- **aborted scans** Fixed the `KeyError` those duplicate jobs raised when they resolved against a parent that had already left the live state map, which surfaced as `Scan failed: RuntimeError: KeyError: '<path>'` and discarded the whole run on wide real-world trees such as a home directory.

## v0.2.17

**Renamed to DiskTide**

- **canonical identity** Renamed the product, Python distribution, import package, primary executable, UI title, diagnostics, documentation, service example, and release artifacts to `DiskTide` / `disktide`.
- **command compatibility** Retained `sizetrail`, `fsmonitor`, and `fsmonitor-cli` as compatibility entry points backed by the same `disktide` CLI, with minimal `sizetrail` and `fs_monitor` import shims for version discovery and `python -m` compatibility.
- **data continuity** New installations use `~/.config/disktide` and `~/.local/share/disktide`; existing `sizetrail` paths take precedence over older `fsmonitor-cli` paths when DiskTide has not initialized its own namespace.
- **cleanup continuity** New quarantine roots use `.disktide-quarantine`, while existing `.sizetrail-quarantine` and `.fsmonitor-quarantine` roots remain protected and discoverable for audit, undo, and purge workflows.

## v0.2.16

**Renamed to SizeTrail**

- **new canonical identity** Renamed the product, Python distribution, import package, primary executable, UI title, diagnostics, documentation, service example, and release artifacts to `SizeTrail` / `sizetrail`.
- **command compatibility** Retained `fsmonitor` and `fsmonitor-cli` as compatibility entry points backed by the same `sizetrail` CLI, and retained a minimal `fs_monitor` import shim for version discovery and `python -m fs_monitor`.
- **data continuity** New installations use `~/.config/sizetrail` and `~/.local/share/sizetrail`; installations with existing `fsmonitor-cli` configuration, monitor history, cleanup rules, or audit data continue using those legacy directories automatically.
- **cleanup continuity** New quarantine roots use `.sizetrail-quarantine`, while existing `.fsmonitor-quarantine` roots remain protected and discoverable for audit, undo, and purge workflows.

## v0.2.15

**History-first Monitor UX and truthful shutdown state**

- **chart-first monitor** Monitor Center now opens directly on History/Trend, keeps the diagnostic text under Details, and adds visible Start sampling / Stop & cancel controls plus a persisted TUI auto-start toggle.
- **honest cancellation** Stopping a foreground monitor host no longer records an expected scan cancellation as a failed monitor or advances the schedule past the uncollected due point.
- **state repair** Dashboard/status reads repair legacy `monitor session stopping` failure projections while preserving the cancelled run audit record.

## v0.2.14

**Event-assisted live state**

- **provisional current view** Added bounded subtree overlays so successful local reconciliation updates Monitor totals and Explorer trees without creating snapshots or feeding history, alerts, compare, exports, or retention.
- **fail-closed convergence** Invalidates provisional state on overflow, root/backend loss, lease expiry, partial scans, hardlink-unique accounting, policy/device uncertainty, excluded filesystems, and overlay bounds; successful full scans clear overlays and advance the canonical base.
- **watch runtime** Added descriptor counts and kernel limits, registration timing/strategy, warnings and fallback reasons, plus session-sticky periodic fallback after runtime backend failure in `auto` mode.
- **startup efficiency** Replaced normal inotify recursive prewalk plus scan with root-first, scan-driven directory registration and added a machine-readable startup/projection benchmark.
- **storage and release** Upgraded monitor status persistence to schema v10 with malformed-JSON fallback and added Wave 15 migration, lifecycle, projection, handoff, fallback, and canonical-side-effect gates.

## v0.2.13

**Cleanup scale and race safety**

- **large-plan runtime** Replaced quadratic parent scans with a sorted ancestor stack and moved normal execution, undo, and purge to normalized action-level persistence with independent plan summaries.
- **quarantine ledger** Added constant-time capacity accounting, pending reservations, crash-state recovery, explicit audit/rebuild commands, and doctor-visible mismatch diagnostics while retaining manifests as recovery evidence.
- **mutation binding** Added short-lived parent/entry identity tokens and POSIX dir-fd mutation checks with post-rename verification and rollback. Direct permanent files use a verified staging unlink; direct permanent directories now fail closed in favor of quarantine then purge.
- **release gates** Added schema-v9 legacy/normalized round trips, adversarial rename/symlink tests, crash-point recovery tests, and a machine-readable 2k/5k/10k plus 600-move benchmark.

## v0.2.12

**Adaptive live scan engine**

- **adaptive workers** Replaced the CPU-oriented default with a path-aware, explainable worker selection that samples at most 64 metadata entries for 75 ms, defaults warm local storage to one worker, retains bounded parallelism for rotational/network/high-latency paths, and preserves exact explicit overrides.
- **giant-directory streaming** Kept one owner per `scandir` cursor while streaming direct entries through a bounded chunk queue, updating aggregates incrementally, publishing geometric live checkpoints, and sorting only when a directory settles.
- **resource scheduling** Replaced the implicit global scan lock with a FIFO `ScanResourcePolicy` that defaults to one active run per filesystem device, supports queued cancellation before collector creation, and exposes queue reason, slot, wait time, and effective worker diagnostics through CLI/TUI/Monitor.
- **observability** Upgraded Monitor status persistence to schema v8, added machine-readable `bench_scan.py --json`, and added the Wave 13 correctness/performance benchmark.

## v0.2.11

**Scalable space-time data plane**

- **targeted history** Added batch path-series resolution across baseline/delta snapshots and changed Monitor history to fetch root plus selected paths in one repository operation while preserving missing, removed, incompatible, partial, pin, and rollup semantics.
- **bounded visualization** Moved interactive diff and Heatmap loading to repository-owned sparse projections and streamed exact changed-path ranking; full snapshot maps and trees are no longer the default visualization input.
- **bounded CPU and memory** Replaced full sibling sorts with heap selection, retained selected branches, emitted exact aggregate remainder totals, and bounded measurement caches by both entries and points.
- **architecture** Moved shared visual formatting out of the TUI view-model package and added an import-boundary regression gate plus a machine-readable Wave 12 benchmark.

## v0.2.10

**Release stabilization and scale correctness**

- **large snapshot safety** Reworked dynamic SQLite path/id lookups and pruning so snapshot save/load/compare and cleanup-plan updates no longer cross the host SQLite bind-variable limit. Automated coverage now round-trips and compares a 100,001-node tree and prunes 100,001 persisted cleanup actions.
- **monitor lifecycle** Replaced the synchronous service-to-TUI callback bridge with Textual's non-blocking message queue, removing the UI-thread/worker shutdown wait cycle while preserving off-screen foreground-host continuation and final `NO_HOST` lease state.
- **metric-correct live views** Propagated the requested `MetricId` through ScanService, ScanEngine, and the bounded live projection. Explorer metric changes now immediately rebuild the in-flight projection, and `monitor run` reports the configured metric instead of always reporting logical bytes.
- **release gates** Added a Linux Python 3.13 full-suite CI job with the `watch` extra and documented core/watch parity as a release prerequisite.

## v0.2.9

**Optional filesystem-event acceleration**

- **optional watch extra** Added `fsmonitor-cli[watch]` with lazy Linux `inotify-simple` discovery. The core wheel remains dependency-isolated and periodic monitoring, CLI, TUI, and doctor continue to work without the extra. `watch --events` is strict; `--periodic-only` is an explicit rollback path.
- **event/reconciliation contract** Added backend-neutral create/modify/delete/move/overflow/root-lost/backend-error events plus a bounded, debounced dirty-path tracker. Ordinary changes trigger policy-aware local `ScanService` reconciliation without writing formal snapshots; scheduled, manual, startup/restart, and overflow recovery remain full authoritative scans.
- **health and persistence** Upgraded SQLite schema to v7 with event mode/backend, watched roots, dirty paths, last event/local/full reconciliation, overflow/recovery counters, degraded reason, and reconciliation confidence. Backend stop, expired lease, root loss, watch limits, and overflow cannot retain stale healthy state.
- **CLI/TUI/config** Added `monitor.event_mode = auto|events|periodic`, `fsmonitor monitor reconcile`, Monitor Center `g` reconciliation, detailed event health in human/JSON status, and banner visibility for dirty/degraded monitors.
- **diagnostics/distribution** Upgraded doctor JSON schema to v3 with watch backend/version/status and install remedy. CI now tests clean core and clean watch-extra environments separately, the distribution verifier checks event modules/ADR/extra metadata, and a copyable systemd user-service example supervises the existing foreground host without creating a second scheduler.

## v0.2.8

**Cleanup intelligence and rule packs**

- **declarative rules** Replaced the Python-owned cleanup catalog with strict TOML schema-v1 packs for Python, Node, Rust, general logs/temp, IDE metadata, and detection-only container/build caches. User packs load from `~/.config/fsmonitor-cli/cleanup-rules`, malformed packs are isolated and reported by doctor, and the schema cannot execute shell or Python code.
- **ranking/explanation** Added deterministic opportunity scoring (35% size, 25% age, 20% inverse risk, 10% rebuildability, 10% rule confidence), explicit confidence/coverage handling, pack provenance, path context, action policy, and rebuild hints. Partial or inaccessible evidence lowers confidence instead of pretending estimates are exact.
- **CleanupPlan v2** Persisted the rule-pack snapshot, score, confidence, coverage, and policy in the existing JSON payload while retaining readers for legacy plans. The SQLite schema remains v6; no migration or snapshot-format change is required.
- **TUI/CLI management** Added a bounded, cached Age/Size Cleanup Map synchronized with the candidate table, a savings-history modal, Settings switches for every pack, and `cleanup rules list|validate|enable|disable`. Small and safe-rendering terminals use a deterministic fallback list.
- **history/purge/alerts** Separated estimated, isolated, purged, actual reclaimed, and undone bytes with `cleanup history --by category|pack|path`. Quarantine purge requires revalidation plus `PURGE <plan-id>` and never manages system Trash. Cleanup-opportunity alerts now read persisted plan summaries, include preview guidance, add a distinct Trend marker, and remain notification-only.
- **safety** Detection-only rules can be discovered and planned but are blocked from safe apply, permanent deletion, and purge. Opportunity score remains an ordering aid and never bypasses Wave 08 identity, boundary, audit, or confirmation checks.

## v0.2.7

**CleanupPlan and safe execution**

- **plan-first cleanup** Replaced CLI/TUI detector-to-delete product paths with one persistent `CleanupService`. Cleanup now defaults to a read-only versioned plan containing rule provenance, risk, identity, estimated bytes, file count, age, planned action, and parent/child overlap resolution.
- **revalidation/safety** Added fail-closed device/inode/type/mtime/size and directory-content revalidation plus rule, age, risk, mount-boundary, scan-root, filesystem-root, database, and quarantine protections. Missing, replaced, modified, symlink-swapped, or no-longer-matching targets are skipped with explicit validation state.
- **trash/quarantine/undo** Added same-filesystem Freedesktop Trash moves with `.trashinfo`, owned mode-0700 quarantine fallback with manifests, capacity/expiry policy, restart-safe undo metadata, and collision refusal. Isolation reports zero actual reclaimed bytes until purge instead of presenting estimates as reclaimed space.
- **audit/persistence** Added schema v6 `cleanup_plans`, `cleanup_actions`, and `cleanup_audit` tables. Validation, pre-execution intent, result, fallback, and undo survive restart; a pre-action audit write failure stops the batch before filesystem mutation.
- **CLI/TUI** `fsmonitor cleanup PATH` now previews only; `--plan ID --apply`, `cleanup history`, and `cleanup undo ID` expose the safe lifecycle. Permanent deletion uses a separate `--permanent` or red TUI path and requires the exact `DELETE <plan-id>` token. CleanupScreen reviews plans and supports safe apply/history/undo without calling the legacy direct-delete helper.

## v0.2.6

**Space-time visualization**

- **visual vocabulary** Added one `VisualState`/`VisualDelta` contract for new, removed, growth, shrink, unchanged, partial, incompatible, and missing paths. Tree, Treemap, Sunburst, Trend, and Heatmap now consume the same Compare/Monitor results instead of independently inferring change semantics.
- **explorer diff** Added Current/Diff switching with `d`, adjacent snapshot-pair browsing with `[`/`]`, stable cursor path across modes/metrics/pairs, delta columns, bounded inline mini trends, removed-path tombstones, and highlighted-path continuity when entering Monitor with `2`.
- **monitor history** Rebuilt the History visual area as Trend (`F1`), Diff Map (`F2`), Growth Rings (`F3`), and Growth Heatmap (`F4`). History supports explicit baseline/target selection, latest/previous reset, root/subtree overlays, time zoom/pan, alert/anomaly/partial/pin/rollup/scan-duration markers, and path navigation from persistent-growth rows.
- **rendering/performance** Added viewport-bounded top-N layout with an aggregate remainder and selected-path protection. A 100,000-node synthetic tree no longer creates 100,000 rectangles/arcs per interaction. Sunburst uses a readable narrow-terminal summary, Heatmap has an 80x24 summary mode, and `NO_COLOR`/safe rendering preserve state through glyphs and grayscale intensity.
- **safety** Incompatible pairs are blocked before layout; partial data is presented as partial confidence; missing and removed Trend points create gaps rather than false zeroes. Diff/measurement caches prevent repeated tree reconstruction or database reads during redraws.

## v0.2.5

**Monitor Center, retention, and alerts**

- **feat** Rebuilt the TUI Monitor screen as Monitor Center with list/detail and narrow-terminal flows for create/edit, pause/resume, run-now, archive, foreground session control, history pins, alert CRUD, retention preview/maintenance, and Explorer selected-subtree continuity.
- **qol/fix** Moved Explorer, Monitor, and FS Overview mode navigation to `1`/`2`/`3`, keeping the mode hint visible without shadowing screen-local actions. Explorer visualization tabs moved to `F1`/`F2`/`F3`, and uppercase `M` opens monitor setup prefilled with the highlighted directory.
- **feat** Added persistent `MonitorDefinition`, status, lease, run, history, and event contracts plus one `MonitorService` shared by CLI and TUI. `watch PATH`, `watch --monitor`, `watch --all`, and the TUI session now use the same no-overlap scan → snapshot → alerts → retention pipeline; the old interval scheduler and alert evaluator were removed.
- **feat** Added `fsmonitor monitor ...` and `fsmonitor alerts ...` command groups with JSON status/list output, foreground host semantics, audited alert checks, and stable alert exit codes.
- **storage** Added schema v5 for monitor definitions/status/leases, monitor-linked scan and snapshot metadata, pins, rollup provenance, retention audit, and alert rules/events v2. Migration remains transactional with a `data.db.pre-v5.bak` recovery backup and backfills legacy alert prototypes without discarding history.
- **retention** Added versioned dense/hourly/daily/weekly retention buckets, revision/latest protection, baseline promotion, explicit pins, maintenance audit, database compaction, and configurable global soft/hard budgets. A hard-budget block prevents only snapshot persistence; scans and Explorer remain available.
- **alerts/history** Added absolute size/growth, percentage growth, free-space, free-inode, and new-large-item rules with metric selection, windows, severity, cooldown, confidence, suppression reason, and immutable events. Subtree history now distinguishes present, missing, removed, partial, incompatible, pinned, and rolled-up points instead of plotting absent data as zero.
- **safety/tests** Kept enabled definitions distinct from active hosts, added expiring repository leases and safe TUI shutdown, serialized full scans shared with Explorer, preserved read-only/degraded history access, and added Wave 06 service, CLI, TUI, retention, alert, budget, migration, race, and 80×24 coverage; the full suite passes with 691 tests.

## v0.2.4

**Snapshot v2 and policy-aware compare**

- **feat** Added snapshot format v2 metadata for metric semantics and availability, scan policy, scanner/run identity, completion and partial/error counts, UTC timestamp rules, and root device/filesystem identity. Baselines and deltas now retain file paths plus logical, allocated, unique, and file-count measurements.
- **feat** Added a persistence-neutral `SnapshotRepository` protocol, SQLite adapter, `SnapshotService`, and `CompareService`. CLI, App, Monitor, and Settings no longer depend directly on the SQLite implementation.
- **feat** Added `fsmonitor compare TARGET BASELINE [PATH]` and `fsmonitor compare --since 7d PATH` with deterministic top growth/shrink, new/removed paths, file-count changes, directory churn, confidence, and compatibility summaries.
- **safety** Compare rejects incompatible root identity, metric semantics/selection, xdev, symlink, hardlink, exclude, and max-depth policies by default. `--raw` is an explicit untrusted override; partial/error coverage remains visible as a warning.
- **migration** Added transactional schema v4 migration with a pre-migration recovery backup, legacy metadata inference markers, rollback-on-failure, and read-only fallback. Existing v0.1.7/schema-v3 snapshots remain listable/loadable and require explicit raw comparison because their policy metadata is incomplete.
- **resilience/tests** Corrupted or unwritable databases no longer block scan-only use and are never auto-deleted. Added migration interruption, legacy fixture, repository, selector, compatibility, CLI, file-level delta, and degraded/read-only tests; full suite passes with 675 tests.

## v0.2.3

**Bounded all-tree scheduling and live Explorer**

- **feat** Replaced top-level-only recursive futures with a bounded all-tree directory scheduler. Every directory is an independent task, executor submissions default to `2 × workers`, the coordinator frontier has a separate hard capacity, and child jobs are materialized lazily from parent cursors.
- **feat** Added deep incremental tree frames with queued directory placeholders, O(1) ancestor contribution deltas, generation-based copy-on-write ownership, and iterative final cloning before hardlink accounting when live frames were published.
- **feat** Added a bounded per-run event mailbox and dispatcher. Pending progress, queued/completed directory, and non-final tree events coalesce while delivered sequence numbers remain contiguous and terminal events are drained exactly once.
- **feat** Explorer now updates Tree as well as Treemap/Sunburst during a scan. Tree nodes update in place with selection identity preserved; visualizations consume a bounded immutable live model rather than the mutable full tree.
- **feat** Scan runs record time-to-first-event/visual, event batches, coalesced and dropped counts, mailbox/frontier high-water marks, and cancellation latency. `tool/bench_scan.py --mode events|live` reports these separately from the stable raw benchmark.
- **compat/tests** Kept `ScanEngine().scan(path)`, the recursive walker, symlink classification cap, metrics, policy markers, cancellation, and tool contracts; added worker-count determinism, frontier bounds, COW frames, slow-consumer pressure, sub-second cancellation, view-model bounds, selection stability, and non-file-linear update tests.

## v0.2.2

**Scan service and events**

- **feat** Added framework-independent `ScanRequest`, `ScanRun`, lifecycle status/phase types, and a sequenced typed event protocol for start, progress, directory checkpoints, aggregate trees, access errors, cancellation, completion, and failure.
- **feat** Added `ScanService` with request validation, platform/metric capability resolution, run-id cancellation, terminal run handoff, consumer exception isolation, event recording, deterministic replay, and progress/tree view-model consumers.
- **refactor** Migrated `fsmonitor scan`, Explorer, periodic `watch`, cleanup discovery, and the monitor scheduler to the same service while retaining `ScanEngine().scan(path)` for benchmark and diagnostic compatibility.
- **fix** CLI/TUI cancellation and failure now have explicit terminal states; cancelled runs never emit completion, consumer failures cannot abort scanning, and Explorer rejects late events from an older run.
- **docs/tests** Added ADR 0002 plus complete/partial/cancel/failure sequence tests, replay tests, CLI exit-code coverage, CLI/TUI result parity, consumer isolation, and static product-boundary checks.

## v0.2.1

**Capabilities and diagnostics**

- **feat** Added `fsmonitor doctor` with human-readable and versioned JSON output covering versions, active platform adapter, redacted XDG paths, database schema/writability, metric support, platform capabilities, optional extras, and default scan policy.
- **feat** Added typed `PlatformCapabilities`, structured probe status/reason/suggestion values, a Linux adapter, and conservative macOS/Windows adapters.
- **fix** FS Overview now displays explicit mount/block-device capability failures instead of silently hiding an empty `lsblk` panel.
- **refactor** Centralized procfs, sysfs, mount-table, block-device, storage-medium, and transform probes under `collectors/platform`; scanner compatibility APIs remain available.

**Distribution**

- **build** Added committed `uv.lock`, locked Python 3.11/3.12/3.13 CI, clean wheel/sdist smoke tests, and verified Textual/Textual Plotext version ranges.
- **build** Added a 20-distribution / 20-MiB / pure-Python core budget gate, package-content verification, SHA-256 checksums, CycloneDX SBOM generation, and build-provenance attestation.
- **docs** Documented `uvx`, `uv tool`, `pipx`, virtualenv installation, upgrades, clean release validation, and PyPI Trusted Publishing.

## v0.2.0

Since `a2872c7` (v0.1.7 release).

**Trustworthy Metrics**

- **feat** Explorer metrics now cycle through **Logical**, **Allocated**, **Unique**, and **Files**. Tree bars and sorting, Treemap area, Sunburst angles, Details rankings, and the header use the same typed metric vocabulary.
- **feat** Scanner nodes record `st_blocks * 512` allocated payload bytes. Sparse files now show the difference between apparent size and actual allocated blocks; platforms without `st_blocks` show `Unavailable` instead of a false zero or logical fallback.
- **feat** Hardlinks are deduplicated in the Unique metric by `(st_dev, st_ino)`. The lexicographically first path owns the bytes, duplicate paths identify that owner, and results are deterministic across worker counts.
- **feat** `fsmonitor scan` gains `--metric logical|allocated|unique|files`, `--one-file-system`, `--cross-filesystems`, `--exclude-pseudo`, and `--include-pseudo`. Completion output includes all measurements, the active scan policy, policy omissions, and hardlink deduplication count.
- **feat** One-filesystem scans leave visible `xdev` boundary nodes. Descendant pseudo-filesystem mounts are excluded by default while an explicitly selected pseudo-filesystem root remains scannable. Max-depth truncation is now visible instead of looking like an empty complete directory.
- **architecture** Added framework-independent `MetricId`, `StorageMeasurements`, and `ScanPolicy` domain types while keeping `FSNode.size` and `ScanEngine().scan(...).size` compatible with existing logical totals.
- **docs/tests** Added ADR 0001 and correctness fixtures for sparse files, hardlinks, device identity, xdev, pseudo mounts, max depth, unavailable allocation data, CLI policy reporting, and four-metric TUI propagation.

## v0.1.7

Since `e174a6a` (v0.1.6 release).

**Branding**

- **change** The product and canonical executable are now named `fsmonitor`. The Python distribution and legacy `fsmonitor-cli` executable remain available for package-index and command compatibility, and the existing XDG config/data directories are retained so settings and snapshot history survive the rename.

**Explorer**

- **fix** The tree's quantitative sort now follows the active bar metric. With the bar toggled to file count (`t`), directories were still ordered by bytes; the default sort now orders by the active metric (count or size), matching what the treemap and sunburst already do, and toggling the metric re-sorts the tree. Name and modified-time sorts remain metric-independent. The sort indicator reflects this (`Sort: Files` when sorting by the count metric).
- **fix** The tree cursor is no longer frozen after a rescan. The tree is `display: none` during a scan, so it loses focus; nothing restored it afterward, leaving arrow keys controlling whatever grabbed focus while it was hidden. The explorer now refocuses the tree once the scan completes, and a reload pins the cursor to the root so it's always live and visible.

**FS Overview**

- **feat** The single **Speed** column is replaced by a **Storage** column of facet badges that break the conflated speed heuristic into orthogonal physical properties. The first badge is the *medium* (`Flash` / `HDD` / `RAM` / `Network` / `?`); it is followed by any detected *transforms* — `RAID` (md device), `Encrypted` (dm-crypt, via a `CRYPT-*` sysfs uuid), `CoW` (btrfs/zfs), and `Compressed` (a `compress=` mount option) — each materially shifting the performance profile yet invisible to the flash-vs-HDD axis. Transforms are detected once at load with no extra I/O. The detail modal gains a matching **Attributes** row. The medium badge already encodes locality, so a network mount shows `Network` with no redundant `Local` badge. The classifier (`sysinfo.storage_class`) is now the single source of truth shared by every view, and newly recognises RAM-backed filesystems (`tmpfs`/`ramfs`), which previously fell through to "Unknown".
- **feat** New opt-in **Benchmark mount** action (`b`): an on-demand throughput probe of the highlighted mount, since the storage-class badges are only heuristics. Because it writes a temporary file, it is gated behind a confirm prompt (press `b` again, muscle-memory friendly) so a stray keystroke never starts disk I/O. The probe atomically creates a unique mode-0600 temp file, writes ≤256 MiB (strictly capped at 25% of free space and refused when less than 1 MiB is safe), `fsync`s, drops the page cache via `posix_fadvise(DONTNEED)`, and reads back cold — reporting buffered-write and approximate cold-read bandwidth without root. The result is recorded per mount for the life of the screen and shown as a **Measured** row when that row is reopened. New module: `scanner/benchmark.py`.
- **fix** "Used" and "Usage %" no longer over-report by the root-reserved block count. The screen computed `used = total - f_bavail`, which folds the unprivileged-user reservation (the default ~5% on ext4) into used space — on a typical root filesystem this nearly doubled the reported usage versus `df` (e.g. 11.4% shown vs 7% real, a 48 GB overstatement). It now follows `df` semantics exactly: `used = (f_blocks - f_bfree) * f_frsize`, `free = f_bavail * f_frsize`, and `usage_pct = used / (used + free)`. The reclaimed gap is exposed as a new **Reserved** row in the per-filesystem detail modal.
- **fix** The aggregate summary (Total / Used / proportional bar) no longer double-counts capacity for devices mounted at more than one path. Bind mounts and btrfs subvolumes report the full pool size from each `statvfs`, so summing raw mount entries inflated the totals; the summary now de-duplicates by backing device first, and its percentage uses the same `used / (used + available)` denominator as the per-mount rows. The per-mount table still lists every mountpoint.
- **fix** A stale NFS/CIFS mount can no longer wedge the loader on the spinner forever. `statvfs` on network filesystems now runs under a 3 s watchdog (local filesystems, which never block, are stat'd directly) and a timed-out mount is skipped.
- **fix** Non-ASCII mountpoints are no longer mojibaked. `/proc/mounts` octal escapes (`\040` etc.) were decoded with `encode('utf-8').decode('unicode_escape')`, which reinterpreted UTF-8 bytes as Latin-1 (`/mnt/café` → `/mnt/cafÃ©`). A targeted octal-only unescape (`unescape_mount_path`) replaces it, shared across `sysinfo` and the overview screen.
- **fix** The usage bar clamps over-100% values (the `*` over-quota case) instead of emitting an over-length bar with a negative empty count.
- **fix** Refreshing (`r`) is now `exclusive`, so mashing the key can't stack overlapping loader threads.

**Block Devices**

- **feat** The FS Overview gains a second **Block Devices** panel that enumerates the whole block layer via `lsblk -J -b`, surfacing storage that `/proc/mounts` + `statvfs` fundamentally cannot see: unmounted-but-formatted filesystems, unformatted partitions, and raw disks with no detected filesystem or child device. Each disk is shown with its partition tree and a colour-coded status (`● mounted`, `○ not mounted`, `○ unformatted`, `○ raw / no filesystem`); the panel header reports disks with no mounted filesystem and their total capacity without implying the contents are safe to reclaim. Row-select opens a per-device detail modal (model, media type, partition list). The panel hides itself when `lsblk` is unavailable. New module: `scanner/blockdev.py`.

**Scanner**

- **fix** `sysinfo._find_block_device` now strips the partition suffix correctly for `mmcblk0p1` → `mmcblk0` (previously `mmcblk0p`) and keeps `loop0` / `dm-0` whole, so the Speed (HDD/SSD) column resolves on eMMC/SD and loop devices.
- **fix** `_find_block_device` no longer mangles software-RAID devices. The trailing-digit strip turned `md0` into `md`, so the sysfs rotational lookup missed and the medium showed "Unknown"; the md number is part of the device identity and is now kept (`md0`, `md127`, partitionable `md_d0`), while md partitions (`md0p1`) strip to the parent like nvme.
- **feat** New facet helpers in `sysinfo`: `classify_medium` (flash/hdd/ram/network/unknown), `detect_transforms` (cheap, no-I/O detection of RAID / dm-crypt / CoW / compression), and `facet_labels` (ordered medium-then-transform badge pairs). These back the FS-Overview Storage column and are consumable individually by downstream tuning.

**Settings**

- **fix** The System Information **Storage** line now uses the shared `storage_class` classifier, so it agrees with the FS-Overview screen. It previously appended only `(HDD)`/`(SSD)` from the rotational bit, ignoring network and RAM-backed mounts entirely.

**Terminal Compatibility**

- **fix** Launching with the standard `NO_COLOR` environment variable no longer crashes during Textual's monochrome render pass. The app selects Textual's no-color-safe ANSI path while still emitting a colorless UI.

**Cleanup**

- **fix** The TUI's **Dry Run** button now actually routes through `delete_targets(..., dry_run=True)` and reports what would be permanently deleted without modifying the filesystem. The previous button dismissed the modal but never invoked the action.
- **fix** A cleanup target that is a symlink to a directory now unlinks the symlink itself. The old `os.path.isdir()`-first branch sent it to `shutil.rmtree()`, which failed on a directory symlink.
- **docs** Cleanup is now described accurately as experimental permanent deletion: there is no trash/quarantine, undo, stale-target revalidation, or persistent audit trail yet.

**Maintenance**

- **remove** Deleted the never-integrated JSON `ScanCache`, its isolated tests, and the no-op `scan --force-rescan` flag. Scans have always gone directly through `ScanEngine`; the app no longer claims an XDG cache directory.
- **remove** Deleted inert configuration keys and Settings controls that were serialized but never affected behavior: `scan.exclude_patterns`, the `[cleanup]` rule/confirmation keys, `ui.default_sort`, and `ui.show_hidden`. Existing TOML files remain loadable because unknown keys are ignored; the retired keys disappear on the next save.
- **remove** Deleted the Explorer's unimplemented `/` search binding instead of leaving a visible no-op action in the key map.
- **docs** Updated the architecture guide to schema v3 (interned paths plus baseline/delta snapshots), refreshed FS Overview and filesystem-touchpoint documentation, and removed obsolete cache references.

**Storage**

- **fix** A full disk no longer crashes launch. The SQLite database is set up eagerly at startup, and its setup requires writes — `os.makedirs` for the data dir (first launch), then `PRAGMA journal_mode=WAL` (the `-wal`/`-shm` sidecars) and the schema migrations. On a full disk any of these raised `OSError`/`sqlite3.OperationalError`, killing the TUI at the exact moment a user needs it to find what's filling the disk. `Database.connect()` now degrades to an in-memory database when the on-disk location is unwritable: the app launches and the explorer/cleanup stay fully usable, only snapshots and history aren't persisted for that session. The post-migration `VACUUM` is now non-fatal for the same reason (it needs temporary space the migration itself already committed without).
- **fix** The degraded database state is now surfaced to the user rather than failing silently or assuming every failure is a full disk. The TUI shows a one-time `Running without persistence` warning on any launch path (welcome screen or straight into the explorer), the Settings screen warns when it can't save (`Settings not saved`), and `scan --snapshot` / `watch` report that snapshots can't be persisted. Degraded `watch` runs no longer accumulate throwaway snapshots in memory, and the database is closed on every exit path.

## v0.1.6

Since `407136d` (v0.1.5 release).

**Explorer**

- **feat** The active visualization tab (Sunburst or Treemap) now renders live as the scan runs, instead of waiting until the end and popping in at once. The engine builds a fresh shallow-copy `FSNode` snapshot after the top-level scandir and again after each top-level subdir worker finishes; the explorer applies that snapshot to whichever viz tab is currently visible. The progress overlay sits in the left tree-panel (where the empty file tree would be) so the entire right side is free for the chart to form, with the dirs / files / size counters ticking next to it.
- **fix** The progress overlay no longer floats in the middle of the screen during a scan; it now occupies the tree-panel (left 40%), which is empty until the scan finishes anyway. This gives the live viz the full right 60% with no occlusion AND gives the progress info a much roomier display area. On scan completion the panel swaps back to the file tree. (Earlier v0.1.6 iterations first wrapped the overlay in a full-screen Container with `background: transparent` — Textual treats transparent-bg widgets as owning their cells, so the live viz rendered correctly into its strip and was immediately painted over — then floated it with `position: absolute` to claim only its own 60x12 footprint, which fixed the occlusion but still ate the middle of the viz panel.)
- **fix** Worker-thread exceptions no longer brick the UI. If `engine.scan()` raised for any reason (e.g. the scan dir was deleted between welcome-screen validation and the worker starting), `_run_scan` never reached `_on_scan_complete` and `_scan_in_progress` stayed True forever, silently gating every subsequent `r` / `u` / `i` / drill-into. The worker now wraps `engine.scan` in try/except and routes failures through a new `_on_scan_failed` handler that resets the same state and surfaces the error via `app.notify`.
- **fix** Previous scan's chart no longer bleeds into a new scan. The viz tabs were only being cleared (`set_node(None)`) when `live_render` was active; with the position-absolute overlay fix above, leftover charts were plainly visible around the centered overlay for the full duration of any non-live rescan. Now every `_start_scan` clears both viz views and the Details panel regardless of mode.
- **fix** `_apply_tree_snapshot` ignores snapshots that arrive when no scan is in progress. Textual's `call_from_thread` preserves FIFO ordering so this can't happen in practice today, but only the runtime contract guarantees it; the defensive guard prevents a stale partial snapshot from overwriting the final tree if that ordering ever changes.
- **style** The viz-switch key hints (`1` / `2` / `3`) now live on the tab labels themselves (`Sunburst [1]` / `Treemap [2]` / `Details [3]`) instead of eating a wide cell in the footer. The bindings still work, they just aren't surfaced in the footer summary anymore.
- **fix** The progress overlay's bar now fills the panel width instead of stopping at ~60%. Textual's `ProgressBar` defaults to `width: auto` and its inner `Bar` defaults to `width: 32`, so the bar was using 32 of the overlay's 54 content cells. Both are now `width: 1fr` inside the overlay.
- **fix** Live frames render at reduced depth (`max_depth=2` instead of 4 for the sunburst and 3 for the treemap), restored on completion. The outer rings of a sunburst are exactly the ones that re-tile every time a subtree's size lands, so dropping them mid-scan removes both the visual jitter and most of the per-frame braille-fill cost.
- **fix** Drill-into is gated while a scan is in flight (`u` go-up, `i` go-into, `r` rescan, and click-to-drill on the tree all return early). The live snapshot's per-subtree aggregates are honest, but the root totals visible on a partial render are not, and we don't want the Details panel showing numbers that contradict themselves a second later.

**Settings**

- **feat** New **Live scan rendering** setting (`ui.live_scan_render`): `auto` (default), `on`, or `off`. `auto` enables live rendering on roomy terminals (>= 80x24) with enough cores (>= 4), and silently stays out of the way on cramped web shells or small VMs where the per-frame redraw cost would compete with the scan. Persisted to `~/.config/fsmonitor-cli/config.toml` only when the user picks `on` or `off`; the default does not pollute the file.

**Scanner**

- **feat** `ScanEngine` gains an optional `tree_callback: Callable[[FSNode], None]` and a `tree_callback_interval` (default 0.25 s). When set, the callback fires once with a top-level-only snapshot before workers start, then at most once per interval as top-level subdir futures resolve, and unconditionally one final time at the end. The first non-forced emit after a force bypasses the throttle so the first ring slice appears the moment a top-level subdir lands, instead of after a full interval of blank viz. Aggregate math is factored into a single `_roll_up` helper used by both the live snapshot path and the final root assembly, so they cannot drift.

**Shutdown**

- **fix** Quit no longer hangs the shell for tens of seconds after a large scan. The previous `_force_teardown` joined worker threads (up to 8s), then `del app` and `gc.collect()` triggered a refcount cascade through the held FSNode tree (5-15s on a 4M-node tree), then `Goodbye!` printed, then Python's natural interpreter shutdown ran atexit handlers + module cleanup + a final cycle GC pass (another 20-30s on the same tree). On a 12 TB / 4M file scan that added up to ~40 seconds of nothing-visible-happening between pressing `q` and getting the shell prompt back. The teardown now does only what matters for correctness — cancel the scan and close the SQLite handle — then `os._exit(0)`. Worker threads are doing read-only FS scans, so killing them mid-syscall is safe; the kernel reclaims the entire FSNode tree's memory in microseconds vs the seconds Python takes to do it with refcounts and finalizers. Measured on a tiny tree: from second-`q` press to process exit is now 214 ms.

**Docs**

- **docs** Architecture guide gains a short subsection on live-scan rendering (the snapshot path, the depth damping, the auto-gate) under Visualization.
- **docs** New blog post [docs/blogs/2026-05-24-reading-the-sunburst.md](blogs/2026-05-24-reading-the-sunburst.md): a short field guide to the chart in the right panel, with the academic lineage of sunburst / radial space-filling visualizations (Shneiderman 1991 treemap, Stasko and Zhang 2000 sunburst), the disk-usage tool lineage that brought it to desktops (Filelight, DaisyDisk, Baobab), how the radial form maps to a Linux filesystem, and a key-by-key reading guide for our implementation.

## v0.1.5

Since `6cb0459` (v0.1.4 release).

**Scanner**

- **fix** Symlink scanning is now one syscall per entry. The walker previously did three (`entry.stat` for the link, `os.readlink` for the target text, `os.stat` to follow the link); on slow shared storage with many symlinks each extra syscall is a server round-trip, blowing up scan times by an order of magnitude. `os.readlink` and the target-following `os.stat` are now deferred to `classify_symlink`, called on demand by the Details panel and the `i` action; the result is cached on the node. Measured on one cluster home (6.1M files, 100 GB on NFS): 2386 s on the pre-fix build, 195 s after, a ~12x recovery and ~20% under the v0.1.3 baseline.
- **feat** On top of that, the engine eagerly classifies the first 100 symlinks it sees at the scan root so a typical `fsmonitor ~` still shows `→ /target` arrows for the handful of links at home root. The cap is hard, so it cannot regress the case where the scan root itself contains hundreds of thousands of symlinks.
- **fix** Directory scans no longer recurse forever on a filesystem cycle. A bind mount or other setup that makes a directory reappear inside itself used to send the scan into an infinite loop; the walker now tracks each directory's `(st_dev, st_ino)` identity along the path from the scan root and stops when a directory is its own ancestor, marking it `(loop)` in the tree and the Details panel. Symlink loops were already prevented; this closes the non-symlink case.

**Progress**

- **fix** Scan progress now updates continuously during a deep scan instead of freezing while a single big subtree is being walked. The walker calls a per-directory tick callback as each directory finishes; the engine folds those into shared live counters and forwards them through the existing `ProgressThrottle` (100 ms coalesce). The earlier behavior only updated when an entire top-level subtree completed, so a scan with one dominant child sat with frozen `Dirs / Files / Size` counters for minutes.
- **fix** The scanning overlay's progress bar is now indeterminate (a pulsing animation with no percentage). The earlier percentage was `completed_top_level_dirs / total_top_level_dirs`, unrelated to actual work, and routinely parked at 97% while one large subtree finished. Without a pre-count pass there is no honest progress fraction; an indeterminate bar plus the now-continuously-moving counters is the honest feedback.

**Tooling**

- **feat** Two diagnostic scripts under `tool/`. `tool/bench_scan.py` is a version-agnostic one-shot that prints `wall-time / dirs / files / size / rate` for any scan path and any prior release (uses only `ScanEngine().scan()`). `tool/diag_scan.py` adds a 1-second heartbeat (with stall detection), a per-directory hotspot table, and an optional `--profile` pass that wraps the scan in `cProfile` and dumps the top callees. Both accept `--workers N` to isolate threading from the picture. These are how the symlink regression above was diagnosed; see [docs/blogs/2026-05-23-symlink-scan-nfs-speedup.md](blogs/2026-05-23-symlink-scan-nfs-speedup.md).
- **add** `tests/test_tools.py` smoke-tests both scripts as subprocesses against a real `tmp_path` and asserts the documented output shape. The scripts also degrade gracefully if the scanner's internal API changes (defensive `getattr` on every field, the diag's monkey-patch wrapped in `try/except` with a stderr notice on failure), so they keep working across releases.

**Docs**

- **docs** Architecture guide, filesystem-call reference, and contributing guide updated for the deferred-symlink-classification behavior and the two new diagnostic scripts.
- **docs** New blog post under `docs/blogs/` walking through the symlink-scan regression hunt end to end: cProfile-driven diagnosis, the fix, measured 12x recovery, and the small UX trade-offs the lazy scheme accepts.

## v0.1.4

Since `96dc76b` (v0.1.3 release).

**Explorer**

- **add** `y` copies the highlighted item's absolute path to the system clipboard. It uses the terminal's OSC 52 escape, so it also works over SSH and in web-based shells that have no local clipboard tool.
- **add** `t` toggles whether proportions are measured by total size (default) or file count. The size-tree bar, sunburst arc angles, treemap rectangle areas, and the Details panel "Top Items" list all follow the active metric. Both `size` and `file_count` are aggregated bottom-up during the scan, so toggling is a re-layout of in-memory data with no extra filesystem access.
- **add** The indicator line above the tree now shows the active sort order and bar metric (e.g. `Sort: Size  Bar: Files`).
- **add** New `fs_monitor/metrics.py` centralises the size/file-count metric vocabulary (`metric_value`, `metric_text`); `compute_layout` and `compute_sunburst` take a `metric` argument so the tree, visualizations, and Details panel stay in sync.

**Symlinks**

- **add** Symbolic links are now first-class in the explorer: the tree shows them as `name → target`, the Details panel reports the target and its type, and broken or file targets are marked. They are never recursed into, so a symlinked directory's bytes never inflate the parent (only the link's own size counts).
- **add** `i` on a symlink whose target is a directory resolves the real path and rescans from there, so linked folders are navigable without the scan traversing the link.
- **fix** A symlink directly under the scan root is now counted as one file, consistent with symlinks deeper in the tree (the engine and walker had diverged).
- **fix** Removed the `follow_symlinks` config option and its Settings toggle, which were never wired into the scanner.

**Docs**

- **docs** User guide, README, and architecture document the `y` / `t` hotkeys and the size/file-count metric.
- **docs** Corrected the stale clone directory (`util_fs_monitor`) in the contributing guide and the viz-tab order in the README key-binding table.

## v0.1.3

Since `1163b24` (Update changelog for quota, viz reorder, and config save fixes).

**Partial Inaccessibility (#14)**

- **add** Walker/engine now track per-node `inaccessible_count` (direct unreadable entries) and `inaccessible_subtree_count` (bottom-up aggregate); previously, per-entry `OSError`s in `os.scandir` and `entry.stat()` were silently dropped, hiding the fact that reported sizes under-counted.
- **add** Bottom-up aggregates `denied_dir_subtree_count` / `partial_dir_subtree_count` on `FSNode` so subtree-wide UI summaries are O(1) instead of re-walking on every status update.
- **add** Size tree marks affected directories with a yellow `◐ N hidden` glyph (full denial keeps its red `⚠`); ancestors with hidden state below get a dim `◐`.
- **add** Info panel gains an **Access** row (Full / Partial / Unreadable) and prefixes the size with `≥` when the subtree is partially scanned.
- **add** Sunburst arc labels and treemap rect labels append `◐` (partial) or `⚠` (unreadable) when there is room.
- **add** Breadcrumb shows the access state of the current directory (`◐` partial, `⚠` unreadable) so it's visible in every viz tab without crowding the sunburst.
- **add** Explorer header subtitle summarises subtree counts: `⚠ N unreadable, ◐ M partial`.

**Web-shell Compatibility**

- **add** UI setting **Safe rendering (web shells)** — when enabled, swaps the block-drawing proportional bar (`█`/`░`) for ASCII (`#`/space) and `⚠`/`◐` accessibility glyphs for `[!]`/`[~]`. Default off; web-based shells (OSC OnDemand, JupyterHub terminals) often lack the Unicode glyphs and render them as runaway horizontal lines.
- **add** All accessibility glyphs (`⚠`, `◐`) now carry a U+FE0E (variation selector-15) suffix forcing *text* presentation, so terminals that would otherwise render them as 2-cell emoji keep them at width 1.

**Navigation / Safety**

- **add** Rescan (`r`) and quit (`q`) now prompt y/n via a confirmation modal to prevent accidental keystrokes from kicking off a long scan or exiting the app. Pressing the originating key (`r` or `q`) twice also confirms — muscle-memory friendly.
- **add** Quarter-screen jumps in the explorer tree: Ctrl+U / Ctrl+D move the cursor by `max(1, height // 4)` rows.
- **add** Settings: Up/Down arrows now move focus between fields (mirrors Tab / Shift+Tab); guarded so an open `Select` dropdown still gets its own arrow-key handling.

**Quit / Shutdown**

- **fix** Pressing `q` on a large scan no longer hangs the shell for many seconds. The walker now propagates the engine's cancel event through every recursive call, so worker threads bail out within ~10 ms instead of running the in-flight subtree to completion. Measured on a 2500-file synthetic tree: cancel-to-finish dropped from "until done" to ~11 ms.
- **fix** Shutdown ordering: cancel active scan → join non-daemon threads (8s cap, warning on expiry) → close the SQLite handle → drop refs → `gc.collect()` → print final goodbye. Previously "Goodbye!" printed before interpreter teardown, so the shell hung on a silent pause; now the goodbye genuinely lands last.
- **fix** Removed the in-TUI "Finishing background tasks..." toast — the screen tore down within the same tick, so the toast was never visible. The terminal-side `Exiting...` / `Goodbye!` messages do the job.

**UI Fixes**

- **fix** Scan progress overlay top border was clipped by the `TabbedContent` panel above it. `ExplorerScreen` now declares an `overlay` layer and centers the overlay in a full-screen invisible container so it floats cleanly above the panels.
- **fix** Explorer subtitle text "denied" replaced with "unreadable" — `node.error` catches any `OSError` (EACCES, EIO, ESTALE, …), not only permission denials.
- **fix** The default `color_theme` is now unified at `warm`. The `UIConfig` dataclass default was already `warm`, but `load_config()` fell back to `default` when a config file omitted the key, so fresh installs and configs predating the `color_theme` setting diverged.

**Code Quality**

- **add** `ConfirmModal(ModalScreen[bool])` in `widgets/confirm_modal.py` — generic y/n dialog with optional `confirm_keys` for caller-supplied muscle-memory keys. Reusable for any future "are you sure?" gate.
- **add** `Database.path` public property; `ExplorerScreen.cancel_active_scan()` public method. Replaces several `self._db._path` / `self._explorer._engine` private-attr reaches.
- **add** New `fs_monitor.glyphs` (`DENIED`, `PARTIAL`, `VS15`, `visible_width()`) and `fs_monitor.rendering` (runtime safe-rendering toggle + glyph/bar getters) modules; every render site consults the helpers so the safe-rendering switch takes effect on the next paint.
- **add** Test pytest-asyncio-free pattern: `asyncio.run(go())` wrapping `app.run_test()`, avoiding the missing-plugin issue that breaks `test_welcome.py`.

**Docs**

- **docs** User guide: documented `q` y/n prompt, `r` y/n prompt, Ctrl+U / Ctrl+D quarter-screen jumps, Settings Up/Down field nav.

## v0.1.2

Since `42fb096` (Update docs for welcome screen and hostname-aware paths).

**Welcome Screen**

- **add** Redesigned welcome screen: single path input with suggestion cycling via Up/Down arrows.
- **add** Suggestion sources: current directory, saved default, last visited, and recent snapshot paths from the database.
- **add** Suggestions are deduplicated; active suggestion highlighted in yellow with an arrow indicator.
- **fix** "Save as default path" was not persisted when the chosen path matched the last-visited path.
- **fix (qol)** "Save as default path" checkbox is now unchecked by default.
- **fix (qol)** Improved welcome screen wording: TUI keys and CLI subcommands are clearly separated.

**FS Overview**

- **add** FS Overview screen (`f`): all real mounted filesystems with mount point, FS type, speed tier (Fast SSD / Medium HDD / Slow Network), size columns, and a visual usage bar.
- **add** FS Overview detail popup: press Enter on any row to see device path, inode stats, block size, and full mount options.
- **add** Proportional summary bar at the top of FS Overview, coloured by speed tier.
- **add** User quota column: displays personal quota usage and limit when the `quota` command is available (supports local and NFS quotas). Detail popup shows soft/hard limits with usage bar.
- **fix** Detail popup no longer resets the table cursor to the first row on close.

**Cleanup**

- **add** `show_cleanup` config flag (`[ui]`); toggle in Settings -> Cleanup Settings.
- **fix (qol)** Cleanup mode is hidden by default; pressing `c` shows a notification instead of silently switching to a destructive screen.

**Quit / Shutdown**

- **add** Exit notification: pressing `q` shows "Finishing background tasks..." toast inside the TUI while background threads wind down; "fsmonitor-cli closed. Goodbye!" is printed to the terminal once fully exited.
- **fix (qol)** Pressing `q` now cancels any in-progress scan, reducing shutdown delay.
- **fix** Config changes (e.g. default visualization) were not persisted when quitting with `q`; config is now saved on quit.

**Fixes**

- **fix** `watch` and `cleanup` CLI commands were passing `/` to `ScanEngine` for FS-type detection instead of the actual scan path -- worker count now adapts correctly per path (e.g. NFS cap applies).
- **fix** Settings screen always showed storage/FS type for `/`; now uses the active scan path.

**Visualization**

- **add** Two new file-type categories: "model" (ML checkpoints: pt, pth, ckpt, onnx, safetensors, pb, tflite, savedmodel) and "log" (log, out, err).
- **add** Expanded extension coverage across all existing categories (60+ new extensions including ML data formats, modern web frameworks, and additional media/archive types).
- **add** Sunburst is now the default visualization and first tab (`1`); treemap moved to `2`.
- **add** `default_viz` config is now applied on explorer mount.

**Docs**

- **docs** User guide: updated welcome screen section, added FS Overview section, added File-Type Categories reference, Cleanup experimental note, updated key binding table, documented `show_cleanup` and per-path worker detection.

## v0.1.1

Since `29b6eaf` (Add file locations section to user guide).

- **add** Database schema v3: path interning (each unique path stored once) and delta storage (only changed directories between snapshots). ~97% size reduction for typical workloads.
- **add** Full baselines every 50 snapshots; deltas in between.
- **add** `strict_path` config option (`[monitor]`) to restrict snapshot matching to exact paths only; toggle in Settings.
- **add** Welcome screen with three path options: current directory, saved default, and last visited. Tab to switch.
- **add** Hostname-aware path storage: paths stored per hostname by default (`hostname_aware_paths` config); toggle in Settings.
- **add** Settings shows database file size and path under System Information.
- **fix** Empty Changes table — `min_delta` threshold was 1 MB, now 0.
- **fix** Trend chart time axis — adaptive format: HH:MM intraday, MM-DD HH:MM multi-day, YYYY-MM-DD for weeks+.
- **fix** Snapshot path matching is now bidirectional: exploring `/data` surfaces watches at `/data/logs` and vice versa.
- **fix** Monitor data loading moved to background thread; added loading indicator to prevent UI freeze.
- **fix (qol)** Snapshot table capped at 200 most recent entries.
- **fix (qol)** Changes title shows the timestamps of the two compared snapshots.
- **fix (qol)** Path completion capped at 200 entries to avoid blocking on large directories (e.g. `/home`).
- **fix (breaking)** Migration v3 clears existing snapshot history. Delete the old database before launching: `rm ~/.local/share/fsmonitor-cli/data.db`
- **fix (breaking)** `VACUUM` run after destructive migration to reclaim disk space.
- **docs** Restructured README: install/uninstall, stored data table, TUI/CLI split, screenshot gallery.
- **docs** Restructured user guide into TUI and CLI sections; added `enabled_rules`/`disabled_rules` config fields.
- **docs** Added `monitor/` module to architecture.md and contributing.md.

## v0.1.0

Initial release (`29b6eaf` and earlier).
