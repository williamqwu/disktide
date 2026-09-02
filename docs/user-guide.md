# User Guide

DiskTide is an interactive terminal tool for exploring disk usage, detecting cleanup opportunities, and tracking how directory sizes change over time.

## Getting Started

Install and launch:

```bash
uv tool install disktide
disktide
```

For a one-shot launch, use `uvx disktide`. `pipx install
disktide` and a normal `pip install disktide` inside a virtual
environment are also supported. The installed command is always `disktide`;
`sizetrail`, `fsmonitor`, and `fsmonitor-cli` remain compatibility aliases.

The welcome screen shows a single path input with a list of suggested starting directories:

- **Current directory** -- the directory you launched `disktide` from
- **Saved default** -- your previously saved default path (if any)
- **Last visited** -- the most recently explored path (if different from the above)
- **Recent** -- paths from previous `watch` or `scan --snapshot` runs

Press **Up/Down** to cycle through suggestions (the active suggestion is highlighted and fills the input). You can also type any path directly. The right arrow key accepts the ghost-text suggestion; completions update live as you type. Press **Enter** to explore.

Check "Save as default path" to remember the current path as your default for next time. Paths are stored per hostname by default, so they stay relevant when sharing a home directory across servers. Set `hostname_aware_paths = false` under `[ui]` to disable this.

## TUI

The interactive TUI uses `1`, `2`, and `3` for Explorer, Monitor, and FS
Overview. Cleanup remains on lowercase `c`. Screen-local actions can therefore
use mnemonic letters without shadowing mode navigation; uppercase `M` in
Explorer sets up monitoring for the highlighted directory. Cleanup is
experimental and disabled by default.

### Explorer (1)

The main view. A file tree on the left shows directories sorted by the active metric, with inline proportional bars. The right panel shows one of three visualizations:

- **Sunburst** (`F1`) -- Concentric rings radiating outward by depth. Each arc's angle represents its share of the parent.
- **Treemap** (`F2`) -- Rectangles sized proportionally to disk usage. Drill into directories by clicking or selecting them.
- **Details** (`F3`) -- Text panel with metadata about the selected file or directory.

Navigation:

| Key | Action |
|-----|--------|
| Up/Down | Move through the tree |
| Ctrl+U / Ctrl+D | Jump up / down by a quarter of the visible tree |
| Left/Right | Collapse/expand tree nodes |
| `u` | Go up to the parent directory |
| `i` | Drill into the selected directory, or a symlinked directory (rescans) |
| `s` | Cycle sort order: size, name, modified |
| `r` | Rescan the current directory (prompts y/n first) |
| `d` | Toggle the current scan and selected snapshot Diff view |
| `[` / `]` | Browse newer / older adjacent snapshot pairs |
| `M` | Set up a persistent monitor for the highlighted directory |
| `y` | Copy the highlighted item's absolute path to the clipboard |
| `t` | Cycle Logical, Allocated, Unique, Files across all views |

The indicator line above the tree shows the current sort order and metric. Press `t` to cycle through:

- **Logical** -- apparent payload bytes from `st_size` (default).
- **Allocated** -- `st_blocks * 512` for every visible path, including every hardlink path.
- **Unique** -- allocated payload with each hardlinked inode counted once.
- **Files** -- regular-file and symlink entry count.

The tree, sunburst, treemap, Details rankings, and header change together. Unique hardlink ownership is finalized after the full scan, so a live in-progress view can temporarily show Unique as unavailable. Platforms without `st_blocks` show Allocated/Unique as `Unavailable`; they are never shown as zero. Press `y` to copy the highlighted item's absolute path to the system clipboard; it uses the terminal's OSC 52 escape, so it works over SSH and in web-based shells where there is no local clipboard tool.

When at least two compatible snapshots exist for the scan root, `d` switches to
Diff mode. Rectangle/ring area continues to represent the target snapshot's
selected metric; color and glyphs represent growth, shrink, new, removed,
partial, or incompatible state. Removed paths remain visible as bounded
tombstones. The highlighted path survives Current/Diff, metric, visualization,
and snapshot-pair changes. Tree rows add an absolute/percentage delta and a
short cached history sparkline. `[`/`]` move through adjacent pairs without
querying SQLite on every repaint.

During a scan, the progress panel shows the scan run id, queued position when
resource constrained, current phase, active policy, effective worker count,
current path, counts, Logical bytes, and rate. Rescan and quit cancel by
run id. Events from an older run are ignored after a newer scan starts, so a
late partial update cannot overwrite the final view from the current run.

Symbolic links are shown as `name → target` and never counted toward folder sizes (only the link's own size). When a link points to a directory, `i` resolves it and rescans from the real location, so linked folders stay navigable without the scan ever traversing the link. Broken links and links to files are marked and cannot be entered.

### Monitor (2)

Monitor Center is the shared setup and management surface for persistent
monitors. Selecting a monitor opens **History** first, with the Trend chart and
snapshot timeline visible immediately. The chart header also says whether
collection is active or stopped and gives the exact host action when an enabled
definition has no owner. Detailed desired state, host activity, health,
event/periodic mode, pending dirty paths, reconciliation confidence,
scan-resource queue/slot and worker reason, and database usage remain available
under **Details**; Alerts and Retention keep their own tabs. Terminals narrower
than 90 columns use a list-first view; press **Enter** for the chart/detail side
and **Escape** to return.

The control bar above History makes foreground hosting explicit. **Start
sampling** hosts every enabled monitor for as long as this TUI remains open;
**Stop & cancel** stops that host and cancels any active monitor scan. The
**Auto-start** toggle persists `monitor.auto_start_in_tui`, so future TUI
launches start the same foreground host automatically. It does not install a
daemon or keep sampling after the TUI exits.

| Key | Action |
|-----|--------|
| `n` | Create a monitor; optionally capture the first snapshot now |
| `e` | Edit path, interval, metric, scan policy, workers, or retention preset |
| `p` | Pause or resume the selected definition |
| `R` | Run the selected monitor now |
| `g` | Run or queue a trusted full reconciliation |
| `S` | Start continuous sampling, or stop it and cancel the active monitor scan |
| _(palette)_ | Archive the monitor after confirmation; history remains — Ctrl+P, "Archive monitor" |
| `i` | Pin or unpin the selected History snapshot |
| `a` / `A` | Add or edit an alert rule in the Alerts tab |
| `x` / Backspace | Enable/disable or remove the selected alert rule |
| `t` | Run retention maintenance from the Retention tab |
| `r` | Refresh all monitor data |
| `1` | Return to Explorer; lowercase `e` remains Edit |
| Tab | Cycle the four history charts (F1-F4 still work where the terminal passes them through) |

The History tab has four visual surfaces:

- **Trend (`F1`)** overlays monitor root and Explorer-selected subtree. Missing,
  removed, and incompatible points create gaps; partial, alert/anomaly, pin,
  rollup, and scan-duration points use markers. Press `z` to cycle time zoom and
  `Shift+Left`/`Shift+Right` to pan.
- **Diff Map (`F2`)** compares latest/previous by default. Highlight a History
  row and press `b` or `v` to choose baseline or target; `l` restores
  latest/previous.
- **Growth Rings (`F3`)** keeps stable path/ring identity while separating
  current area from the growth overlay. Narrow terminals display a Tree/Treemap
  fallback summary instead of an unreadable circle.
- **Heatmap (`F4`)** ranks paths by repeated positive intervals before one-time
  spikes. Rows are paths, columns are bounded snapshot intervals, and Enter on a
  row changes the selected subtree context. At 80x24 it switches to a concise
  persistent-growth summary.

Changing the root, selected metric, or scan policy creates a new monitor
revision. Older history remains visible, but incompatible revision segments are
not joined into a trusted trend. Explorer passes its root and actual
cursor-highlighted path to Monitor Center, so History can show both the monitor
root and the selected path with explicit present, missing, removed, partial,
pinned, and rollup state.

Definitions are persistent; execution is not. `enabled · no-host` means the
definition is ready but no process currently owns it. Press `s` in the TUI or
run `disktide watch --monitor/--all` to host scans. Leaving Monitor Center for
Explorer keeps the TUI session alive, while quitting the app stops it and
releases its lease. With `disktide[watch]` installed on Linux, `auto` mode
attaches inotify to each held lease. Ordinary events trigger bounded local
reconciliation; startup, restart, overflow, backend loss, manual `g`, and the
normal interval trigger full reconciliation. Events never replace the periodic
full-scan source of truth.

After a successful local reconciliation, Monitor and Explorer show a clearly
labelled **provisional** current value/tree with its canonical base snapshot,
timestamp, confidence, dirty paths, and overlay size. History, compare, alerts,
retention, and exports remain canonical-only. Watch status also reports actual
descriptor usage, kernel limits, registration strategy/time, warnings, and any
periodic fallback reason. A restart or uncertain local result invalidates the
provisional view until the next full reconciliation succeeds.

### FS Overview (3)

Shows all mounted real filesystems at a glance. Press `3` to open.

The top bar summarises total mounted space and usage percentage. Aggregate capacity is de-duplicated by backing device so bind mounts and btrfs subvolumes do not inflate the total. The table still lists every mountpoint:

| Column | Description |
|--------|-------------|
| Mount | Mountpoint path |
| FS Type | Filesystem type (ext4, xfs, nfs4, etc.) |
| Storage | Medium badge (`Flash`, `HDD`, `RAM`, `Network`, or `?`) plus detected transforms such as `RAID`, `Encrypted`, `CoW`, and `Compressed` |
| Total / Used / Free | Disk space |
| Usage | `df`-style visual bar + percentage |
| Quota | Current user's used/hard-limit values when the platform reports them |

When `lsblk` is available, a second panel shows block devices and partitions, including mounted, unmounted, unformatted, and raw devices. An unmounted or raw device is **not** presented as safe-to-reclaim space; select a row to inspect details.

Mount and block-device data comes from the active platform adapter. If procfs,
sysfs, `lsblk`, or device permissions are unavailable, this screen shows the
capability status and reason instead of failing or silently presenting an empty
panel.

Press `Enter` on a filesystem or block-device row to open its details. Press `b` on a filesystem row to run an opt-in throughput probe after a second confirmation. The probe creates a mode-0600 temporary file, writes at most 256 MiB and never more than 25% of currently available space, then removes the file. The displayed read result is approximate because cache eviction is advisory.

Pseudo-filesystems (`proc`, `sysfs`, `tmpfs`, etc.) are automatically filtered out. Press `r` to refresh.

### Cleanup (4)

Detects pattern-matched candidates through versioned declarative rule packs for
Python, Node, Rust, general logs/temp, IDE metadata, and container/build caches.
Every row exposes pack/version, category, reason, age, score, confidence, risk,
default action policy, and rebuild guidance. These rules and scores are
heuristics, not a guarantee that a path is safe to remove.

Cleanup mode is **disabled by default**. Enable it under "Cleanup Settings" in the Settings screen (`,`) — a collapsed section near the bottom; move to its title and press Enter to open it. Once enabled, press `4` to switch to it.

The Age/Size Map places age on the vertical axis and size on the horizontal
axis, with glyph/color conveying risk and the selected point synchronized with
the table. Press `m` to focus the map and use its arrow keys plus Enter to move
the table cursor. On small or safe-rendering terminals it becomes a bounded
score/age/confidence list instead of dropping information.

Review every selected path before acting. Press `p` to create and inspect a
persistent CleanupPlan v2; this is read-only until an explicit action is chosen.
**Apply Safely** moves each revalidated target to system Trash when an atomic
same-filesystem move is available, otherwise to an owned mode-0700 quarantine
directory next to the target. Press `z` to restore the latest recoverable plan
and `h` to inspect savings history grouped by category. Permanent deletion is a
separate red action and requires typing the exact plan-scoped
`DELETE <plan-id>` token. Detection-only rules remain visible but cannot enter
safe apply, permanent deletion, or purge.

Before every action, disktide repeats `lstat`, identity, rule, age, directory
content, mount-boundary, and protected-path checks. Changed, missing, replaced,
or no-longer-matching targets are skipped with a specific audit reason. Parent
targets subsume matching children so estimated bytes and execution are not
double counted. Trash/quarantine isolates data but reports actual reclaimed
bytes as zero until the data is purged.

### Mouse

Mouse support is on by default and never replaces a keyboard path -- every
click routes into the same navigation a keystroke would, with the same guards.

| Gesture | Action |
|---------|--------|
| Click a tree row | Move the cursor there; a directory drills in |
| Click a sunburst arc / treemap rectangle | Select that path in the tree; a directory drills in |
| Click the sunburst centre | Go up one level within the scanned tree |
| Wheel | Scroll the tree and the Details panel |
| Hover a chart shape | Show its name, size, and share of the chart |
| Click a tab, button, or footer entry | Activate it |
| Shift+drag | Your terminal's own text selection |

Clicking the sunburst centre never rescans. The keyboard `u` deliberately
rescans from the parent directory once you are at the scan root; a stray click
must not be able to start a long scan, so at the scan root a centre click does
nothing. Clicks are ignored while a scan is in flight, for the same reason
keyboard navigation is: the live tree's aggregates are still settling. An
aggregate "… N more" arc or block stands for several directories at once and is
not a navigation target.

Hovering only hit-tests the chart and updates the tooltip; it never recomputes
the layout. Moving the tree cursor brightens the matching arc once the cursor
settles, so holding an arrow key does not pay for a chart rebuild per repeat.

To turn it off, launch with `disktide --no-mouse` for a single session, or set
`mouse = false` under `[ui]`. The **Mouse support** switch in Settings writes
the same key. Mouse reporting is negotiated once, when the terminal enters
application mode, so the switch takes effect the next time DiskTide starts --
Textual offers no supported way to change it under a running app.

### Key Binding Reference

Press `?` in the app for this table, live, for the screen you are on — it is
generated from the bindings themselves, so it cannot fall out of date the way
this one can. It lists the keys that work everywhere first, then the current
screen's own, in sections: **Move around**, **Choose the view**, **Compare
snapshots**, **Alerts**, **Other actions**. The keys that are too niche for
the footer — `g` for the ring shape, `[`/`]` for snapshot pairs, `z` and
Shift+arrows for the trend chart — live there, filed with the everyday keys
they belong with. `Ctrl+P` searches every action by name, including the few
that carry no key at all.

| Key | Scope | Action |
|-----|-------|--------|
| `1` `2` `3` `4` | Global | Switch to Explorer / Monitor / FS Overview / Cleanup |
| `?` | Global | Open the key map for the current screen |
| `,` | Global | Open Settings |
| Ctrl+P | Global | Command palette — search every action by name |
| `q` | Global | Quit (prompts y/n first) |
| `r` | Explorer, Cleanup, Monitor, FS Overview | Rescan / refresh |
| Esc | Modals, Monitor detail | Back / close |
| `F1` / `F2` / `F3` | Explorer | Sunburst / Treemap / Details |
| `u` / `i` | Explorer | Navigate up / drill into directory |
| `s` | Explorer | Cycle sort order |
| `d` | Explorer | Toggle Current/Diff view |
| `t` | Explorer | Cycle Logical / Allocated / Unique / Files across all views |
| `y` | Explorer | Copy highlighted path to clipboard |
| `M` | Explorer | Set up monitoring for the highlighted directory |
| `[` / `]` | Explorer | Browse newer / older adjacent snapshot pairs |
| `g` | Explorer | Cycle the ring chart's shape: tiles / disc / fill |
| Ctrl+U / Ctrl+D | Explorer | Jump tree cursor up / down by a quarter screen |
| `n` / `e` / `p` | Monitor | New / edit / pause-resume the selected monitor |
| `R` | Monitor | Run the selected monitor now |
| `S` | Monitor | Start or stop continuous sampling |
| `g` | Monitor | Run or queue a trusted full reconciliation |
| `b` / `v` | Monitor | Set the diff baseline / target snapshot |
| Tab | Monitor | Cycle the four history charts |
| `B` | FS Overview | Confirm and benchmark the highlighted mount |
| Enter | FS Overview | Open filesystem or block-device details |
| Space / `a` | Cleanup | Toggle row selection / select all |
| `p` | Cleanup | Create and review a CleanupPlan for selected rows |
| `z` | Cleanup | Undo the latest recoverable plan |
| `h` | Cleanup | Show persisted savings history by category |
| `m` | Cleanup | Focus the synchronized Age/Size Map |
| Up / Down | Settings | Move focus between fields (also Tab/Shift+Tab) |

Six keys moved in this release. If your fingers disagree, you do not have to
relearn them -- see below.

### Remapping keys

Every binding carries an id, and `[keys]` in `config.toml` maps an id to a key.
Three presets ship:

| Preset | What it is |
|--------|------------|
| `spine` | The layout above. The default. |
| `safe` | The v0.2.30 layout with only the three consequence mismatches fixed (`d`, `u`, `s`). |
| `classic` | The v0.2.30 layout exactly. |

```toml
[keys]
preset = "classic"          # start from the old layout ...
"cleanup.review_plan" = "p" # ... but keep the new plan key
```

Overrides apply on top of the preset, so "classic except one key" is two lines.
The key map (`?`) shows which preset is live and always reflects your own
bindings. An id that no longer exists costs you that one binding and a warning
toast, never the session.

Why the six moved:

| Was | Now | Reason |
|-----|-----|--------|
| `c` Cleanup | `4` | Modes are digits, with no exception to remember. |
| `?` Settings | `?` key map, `,` Settings | `?` means help everywhere else; there was no key map in the app at all. |
| `d` Cleanup review plan | `p` | `d` toggled a harmless view in Explorer and reviewed a delete plan here. |
| `u` Cleanup undo | `z` | `u` steps up a directory in Explorer and undid a plan here. |
| `s` Monitor sampling | `S` | Starting a background sampling host is consequential; Explorer's `s` only sorts. |
| `b` FS benchmark | `B` | Benchmarking writes to the disk under test, and frees `b` for "baseline". |

Archiving a monitor lost its key entirely (it was `d`). It is rare, it is
destructive, and it is now in the command palette by name.

## CLI Commands

These commands run outside the TUI. Results go to stdout; run status,
progress, warnings, and errors go to stderr, so `disktide scan / > report.txt`
captures the report alone and still shows progress on the terminal. The live
progress counter is drawn only when stderr is a terminal.

Commands that accept `--json` write one JSON document to stdout and nothing
else, which makes them safe to pipe into `jq`.

The TUI, the one-shot CLI, and periodic `watch` runs all drive the same scan
service, so a run started from any of them reports the same run id, phase,
policy, and terminal status.

### doctor

Report the installed version, Python/Textual versions, active platform adapter,
terminal identity and cell geometry, application paths, database status/schema,
storage metrics, platform capabilities, optional extras (including watch
backend/version/status), and default scan policy:

```bash
disktide doctor
disktide doctor --json
```

The JSON schema is versioned and suitable for attaching to issue reports. App
paths are represented as `~` or `$XDG_*` paths by default; use `--show-paths`
only when raw local paths are intentionally required. The report never walks a
scan tree or lists user files.

### scan

One-shot scan with a text summary:

```bash
disktide scan /path
disktide scan /path --snapshot      # save results to the database
disktide scan /path -d 5 -w 4       # limit depth to 5, use 4 threads
disktide scan /path --metric allocated
disktide scan / --metric unique --one-file-system --exclude-pseudo
disktide scan /path --json          # machine-readable, stdout only
```

`--metric` controls the completion total, sorting, and top-directory bars.
Every completion summary still prints Logical, Allocated, and Unique together.
Cross-filesystem scanning is the default; `--one-file-system` leaves visible
`xdev` boundary nodes. Descendant pseudo-filesystem mounts are excluded by
default, while an explicitly selected pseudo root is still scanned. Use
`--include-pseudo` to opt in to descendant pseudo filesystems.

The command reports a short run id, phase, policy, and one explicit terminal
status (`completed`, `partial`, `cancelled`, or `failed`) on stderr, and writes
the summary and top-directory table to stdout. Exit codes
are `0` for complete or partial success, `1` for scan failure, `2` for invalid
input, and `130` for cancellation. A partial result remains usable but includes
an explicit coverage line; cancellation never prints a completion summary.

`--json` replaces the text report with a versioned document carrying the run
id, status, policy, worker selection, all three byte totals, coverage counts,
and the direct child directories sorted by the selected metric. It silences the
stderr narration, and a cancelled or failed run still yields a parseable
document alongside its non-zero exit code.

### compare

Compare a target snapshot with a baseline snapshot:

```bash
disktide compare latest previous /path
disktide compare 42 41
disktide compare --since 7d /path
```

The first selector is the target and the second is the baseline, so the report
reads `baseline → target`. Selectors can be `latest`, `previous`, `oldest`, or a
numeric snapshot id. `--since` compares the latest snapshot with the newest
snapshot at least that old.

The report includes logical/allocated/unique and file-count totals, top growth
and shrink, new and removed paths, directory churn, and partial/error
confidence. Snapshot format, root identity, metric semantics/selection, xdev,
symlink, hardlink, exclude, and max-depth policy must be compatible. An
incompatible comparison exits with status 2 and does not present a trusted
growth result. `--raw` explicitly requests an untrusted diagnostic diff and
keeps every incompatibility visible in the output.

### monitor

Create and manage the same persistent definitions used by Monitor Center:

```bash
disktide monitor add /data --label data --interval 6h --capture-now
disktide monitor list
disktide monitor status 1
disktide monitor edit 1 --interval 1h --metric allocated
disktide monitor pause 1
disktide monitor resume 1
disktide monitor run 1
disktide monitor reconcile 1
disktide monitor retention 1          # preview
disktide monitor retention 1 --apply  # run maintenance
disktide monitor pin 42 --label release
disktide monitor unpin 42
disktide monitor remove 1             # archive; keep history
```

Monitor identifiers may be numeric ids or labels where the command accepts an
identifier. `monitor list/status --json` provide scripting output. A saved
definition records its own interval, metric, scan policy, workers, revision,
desired state, and retention policy in SQLite; editing Settings does not rewrite
existing definitions.

### alerts

Alert rules belong to one monitor and share the same repository/service path as
the TUI Alerts tab:

```bash
disktide alerts add 1 /data --size 500GiB
disktide alerts add 1 /data/logs --growth 10GiB --window 24h
disktide alerts add 1 /data --percent 20 --cooldown 6h
disktide alerts add 1 /data --free-space 50GiB --severity critical
disktide alerts add 1 /data --inode-free 100000
disktide alerts add 1 /data/incoming --new-large 4GiB
disktide alerts list 1
disktide alerts check 1
disktide alerts disable RULE_ID
disktide alerts enable RULE_ID
disktide alerts remove RULE_ID
```

Rules can evaluate logical, allocated, unique, or file-count measurements.
Events retain old/new snapshot ids, observed value, threshold, severity,
confidence, cooldown suppression, and suppression reason. `alerts check` exits
with status 2 for an unsuppressed trigger, 3 when events exist but all are
suppressed, and 0 when nothing triggers.

### watch

Run the shared monitor host in the foreground until interrupted or
`--max-time` is reached:

```bash
disktide watch /path                   # transient definition; default 6h
disktide watch /path --interval 1h
disktide watch /path -i 30m -t 12h
disktide watch /path --events          # require disktide[watch]
disktide watch /path --periodic-only   # force the dependency-free core path
disktide watch --monitor 1             # host one saved definition
disktide watch --all                   # host all enabled definitions
```

The default `auto` mode uses the native backend when installed and otherwise
prints a periodic fallback reason. `--events` is strict and exits non-zero with
an installation suggestion when the backend is unavailable. `--periodic-only`
never imports the optional dependency.

Periodic/manual/full-recovery runs print their run id, policy, terminal status,
duration, and snapshot result. Event-driven local reconciliation uses the same
`ScanService` and policy contract but does not write an intermediate formal
snapshot; only full reconciliation updates history, compare baselines,
retention, and alerts. `watch PATH` does not silently create a persistent
definition; saved monitors use their stored retention policy, alerts, revision,
and schedule. A repository lease prevents a TUI, CLI, and supervised user
service from running the same monitor concurrently.

Since `watch` runs in the foreground, use tmux or another external supervisor
when it must outlive the current shell:

```bash
# tmux (recommended -- reattach later with `tmux attach -t fsmon`)
tmux new -s fsmon
disktide watch /path --interval 6h

# nohup (background, no reattach; host every enabled saved monitor)
nohup disktide watch --all > /dev/null 2>&1 &
```

An external systemd user unit can supervise the same foreground host. The
project ships `docs/examples/disktide-watch.service` as a copyable example; it
does not install, enable, or grant cleanup permissions to that unit:

```ini
# ~/.config/systemd/user/disktide.service
[Service]
ExecStart=%h/.local/bin/disktide watch --all
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now disktide.service
loginctl enable-linger $USER
```

Use `--events` in the unit only after installing `disktide[watch]`. Omit it
for automatic fallback, or use `--periodic-only` to guarantee core-only hosting.
Every backend start/restart is recorded as requiring full reconciliation, so
service downtime is never presented as a complete event history.

### cleanup

`cleanup` is a command group. `disktide cleanup PATH` is shorthand for
`disktide cleanup plan PATH`, and `disktide cleanup --help` lists every
subcommand (`plan`, `history`, `undo`, `purge`, `rules`, `quarantine`), each
with its own `--help`.

Cleanup defaults to a persistent, read-only preview:

```bash
disktide cleanup /path
disktide cleanup --plan PLAN_ID --apply
disktide cleanup history --by category
disktide cleanup undo PLAN_OR_ACTION_ID
disktide cleanup purge PLAN_OR_ACTION_ID
disktide cleanup quarantine audit /path/.disktide-quarantine
disktide cleanup quarantine rebuild /path/.disktide-quarantine
```

The first command scans, resolves parent/child overlap, saves the plan, and makes
no filesystem changes. `--apply` revalidates each target and uses Trash with
same-filesystem quarantine fallback. `history` distinguishes estimated,
isolated, purged, actually reclaimed, and undone bytes; `undo` refuses to
overwrite a newly created original path. Plans and per-action audit events
survive process restart. Database schema v9 stores plan metadata and ordered
action rows separately, so one action update does not rewrite a large plan; the
versioned CleanupPlan JSON payload remains v2 and legacy payloads remain readable.

`history` can group those values by `category`, `pack`, or `path`. `purge` only owns
revalidated quarantine content and requires the exact `PURGE <plan-id>` token;
it never purges system Trash. Quarantine capacity uses `.ledger.json` rather
than rescanning every manifest before every move. `quarantine audit` reports a
ledger/manifest mismatch, while `quarantine rebuild` safely reconstructs the
summary and interrupted transition state without deleting unknown files.

Manage declarative rule packs from the same configuration used by the TUI:

```bash
disktide cleanup rules list
disktide cleanup rules validate /path/to/pack.toml
disktide cleanup rules disable node
disktide cleanup rules enable node
```

Built-in packs are packaged with the application. Optional user packs load from
`~/.config/disktide/cleanup-rules/*.toml` (respecting `XDG_CONFIG_HOME`).
Validation is strict: unknown fields, unsupported schema versions, invalid
identifiers/types, duplicate names, and executable hook fields are rejected.
One bad pack is isolated and reported by `disktide doctor`; valid packs remain
available.

Permanent deletion is intentionally separate:

```bash
disktide cleanup --plan PLAN_ID --permanent
```

The command prints the irreversible warning and requires the exact
`DELETE <plan-id>` token. Supported POSIX systems bind file deletion to a
short-lived parent/entry identity token and a verified staging unlink. Direct
permanent directory deletion is blocked; use quarantine followed by confirmed
purge. Platforms without the required dir-fd primitives block permanent
deletion. `--apply` can never select permanent deletion.

## Configuration

New installations store settings in `~/.config/disktide/config.toml`
(respecting `XDG_CONFIG_HOME`). If the DiskTide path does not yet contain a
configuration, DiskTide continues using
`~/.config/sizetrail/config.toml` or, for older installations,
`~/.config/fsmonitor-cli/config.toml`. This preserves settings and user cleanup
rules across both previous names. Edit the active file directly or use the
settings screen (`?` in the TUI).

```toml
[scan]
max_depth = 10                           # omit for unlimited
workers = 4                              # omit for auto-detect
# one_file_system = true                 # default: false
# exclude_pseudo_filesystems = false     # default: true

[monitor]
default_interval = 21600                 # 6 hours, in seconds
# max_watch_time = 86400                # optional cap in seconds
database_soft_budget = 2147483648        # 2 GiB; trigger maintenance
database_hard_budget = 3221225472        # 3 GiB; block new snapshots after maintenance
# auto_start_in_tui = true              # default false; host enabled monitors on launch
event_mode = "auto"                      # auto | events | periodic

[cleanup]
# prefer_trash = false                   # default true; false uses quarantine directly
quarantine_retention_days = 7
quarantine_max_bytes = 10737418240        # 10 GiB capacity policy
# disabled_rule_packs = ["node"]          # shared by CLI and TUI
map_max_points = 80                       # bounded Age/Size Map (10–500)

[ui]
color_theme = "disktide"                 # disktide, cold, colorblind, cyberpunk, mono
default_viz = "sunburst"                 # treemap, sunburst, details
# show_cleanup = true                    # enable Cleanup mode (disabled by default)
# safe_rendering = true                  # ASCII glyphs and block-free charts for web shells
# cell_aspect = 2.43                     # omit to measure; pixel height/width of one cell
# ring_shape = "disc"                    # default tiles; disc | fill | tiles (see Ring shape)
# mouse = false                          # default true; --no-mouse overrides per session
# live_scan_render = "auto"              # auto | on | off — draw viz live during scan
# default_scan_path = "/home/user/data" # pre-fill welcome screen
# hostname_aware_paths = false           # store paths per hostname (default: true)
```

Per-monitor paths, schedules, scan policy, desired state, revisions, retention,
pins, and alerts live only in the SQLite repository. Manage them through
Monitor Center or the `monitor`/`alerts` commands. The `[monitor]` config section
contains global defaults, database budgets, the foreground watch cap, TUI
auto-start preference, and event backend policy. `events` requires the optional
extra; `auto` safely falls back; `periodic` disables native events.

The `[cleanup]` section controls safe executor preference, quarantine expiry and
capacity, disabled declarative packs, and the Age/Size Map point budget. It
never enables permanent deletion as a default action. Settings presents the
same pack switches with source, rule count, and maximum risk.

### Terminal cell aspect

The treemap's "square" rectangles, and a ring chart drawn as a `disc`, are the
right shape only if DiskTide knows how tall one character cell is per unit of
width. (The default `tiles` ring shape needs none of this -- see **Ring shape**
below -- but the treemap always does.) That ratio
depends on the font and its line spacing: a 7x17 pixel cell -- a 14px monospace
face at 1.2 line height -- is 2.43, and drawing a circle as if it were the
traditional 2.0 stretches it vertically by about 21%, which reads as a plainly
oval disc.

DiskTide resolves the ratio in order, and stops at the first answer:

1. `DISKTIDE_CELL_ASPECT`, e.g. `export DISKTIDE_CELL_ASPECT=2.43`.
2. `cell_aspect` under `[ui]`, which is what the Settings box writes.
3. An **in-band resize report** (terminal mode 2048), which carries the window's
   pixel size with every resize.
4. **`TIOCGWINSZ`** pixel fields, which most native terminals fill in and which
   tmux forwards into every pane it owns.
5. An **XTWINOPS probe** (`CSI 16 t` / `CSI 14 t`) sent once at startup, only
   when nothing above answered. Set `DISKTIDE_NO_TERMINAL_PROBE=1` to skip it.
6. **2.0**, the historical assumption, when nothing measured anything.

Most native terminals -- kitty, foot, Alacritty, GNOME Terminal and other VTE
terminals, xterm, WezTerm, Ghostty, Konsole, iTerm2 -- are measured
automatically by step 3 or 4 and need nothing from you. The families that report
no pixel size at all, and so land on the 2.0 fallback, are the xterm.js-based
web shells (JupyterLab's terminal, Open OnDemand, ttyd), VS Code's integrated
terminal, ConPTY and Windows Terminal (including WSL and ssh launched from it),
mosh, GNU screen, a detached tmux or one whose client reports no pixel size, the
`textual serve` web driver, and any wrapper pty. In those, set the value by
hand.

To calibrate, open **Settings ▸ Cell aspect** (`,`). It takes a number
directly and shows what was measured beside it; blank the box to go back to
automatic detection. For converging on a circle by eye, the command palette
(Ctrl+P) offers **Cell aspect: rounder** and **Cell aspect: taller**, which
move the ratio by 0.05, apply immediately and are saved.

These two were `,` and `.` in the Explorer until v0.2.31. They were retired
because `tiles` is now the default ring shape and renders identically from
aspect 1.5 through 3.0, which makes this a `disc`-and-treemap setup step
rather than something worth two top-level keys -- and `,` is worth more as
the Settings key.

`disktide doctor` prints a **Terminal** block naming which mechanism is feeding
the value, whether each pixel-size report answered, and -- when nothing did --
what to do about it.

### Ring shape

The ring chart draws in one of three shapes, chosen by `[ui] ring_shape` or
cycled in the Explorer with `g`. A press applies at once and is saved, so a
shape you prefer survives the next launch.

| Shape | Rings are | Siblings are cut by |
| --- | --- | --- |
| `tiles` (default) | rectangles sized in whole cells | straight lines |
| `disc` | circles | rays from the centre |
| `fill` | rectangles stretched to the pane's edges | rays from the centre |

A terminal cell is a rectangle, so a circle built out of cells is an
approximation at every point of its rim: the disc's silhouette, its centre hole
and all four of its ring boundaries are anti-aliased, which is how a chart made
of cells admits an edge falls between two of them. `tiles` sizes each ring as a
whole number of columns and half-rows and cuts siblings with straight lines, so
every edge in the picture lands on a cell edge and no cell is a blend of two
things. It is the same at every cell aspect, which is why it needs no
calibration; `disc` and `fill` do.

What `tiles` gives up is the radial reading. A child's cut and its parent's sit
at the same fraction of two different loops, so they line up along a face and
step where a loop turns a corner: the chart reads as nested frames rather than
as rays fanning out from the root. Pick `disc` if that alignment is what you
use the chart for. Area is exactly proportional to value in all three.

The **Live scan rendering** setting (`live_scan_render`) controls whether the active visualization tab redraws as the scan progresses. `auto` (the default) enables it on terminals at least 80 columns by 24 rows with at least 4 CPUs, and stays off on smaller / lower-resource setups where the per-frame redraw cost would compete with the scan. Set to `on` to force it regardless of terminal size, or `off` to wait for the scan to finish and render once.

All fields are optional. Missing values use sensible defaults. When `workers`
is omitted, the scanner combines CPU/load/memory information with filesystem
and storage hints plus a bounded metadata sample of the actual scan path. Warm
low-latency local paths normally use one worker, rotational paths at most two,
and network or measured high-latency paths at most four. An explicit value is
used exactly. CLI and Monitor status show the effective count and reason.

## File-Type Categories

The treemap and sunburst visualizations color files by category. Each file's extension determines its category; six categories carry a color, and anything unrecognized stays a neutral gray.

| Category | Extensions | Typical use |
|----------|-----------|-------------|
| code | py, js, ts, tsx, jsx, vue, c, cpp, h, java, go, rs, rb, sh, css, html, kt, swift, lua, r, php, scala, ipynb | Source code and notebooks |
| docs | pdf, doc, docx, odt, tex, txt, md, rst, rtf, epub, ppt, pptx, xls, xlsx, json, yaml, yml, toml, xml, ini, cfg, env, conf, properties, lock | Documents, presentations, configuration |
| data | csv, sqlite, db, sql, parquet, npy, npz, h5, hdf5, pkl, pickle, jsonl, arrow, feather, tfrecord, lmdb, pt, pth, ckpt, onnx, safetensors, pb, tflite, savedmodel | Datasets, serialized data, model checkpoints |
| media | png, jpg, jpeg, gif, svg, bmp, webp, ico, tiff, tif, heic, avif, raw, mp3, mp4, wav, avi, mkv, flac, ogg, aac, mov, webm, m4a, m4v | Images, audio, video |
| archive | zip, tar, gz, bz2, xz, 7z, rar, zst, lz4, deb, rpm, iso | Compressed archives and packages |
| ephemeral | o, so, pyc, class, whl, egg, dll, lib, a, obj, jar, war, log, out, err | Build artifacts, caches, logs — the reclaimable stuff |
| other | *(everything else)* | Unrecognized extensions |

Files without an extension (e.g., `Makefile`, `Dockerfile`) are classified as "other".

One thing overrides the extension: everything inside a known regenerable container — `.venv`, `venv`, `node_modules`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.nox`, `.eggs`, `.cache` — counts as ephemeral wholesale, files and directories alike, because a `.py` under `site-packages` is installed payload rather than your code and the whole tree comes back from one reinstall. Ambiguous names like `build` and `dist` are deliberately left to their extensions; they are as often yours as a tool's.

Six is the ceiling at which the colors stay distinguishable from each other in a colorblind simulation as well as in normal vision, so pairs that call for the same cleanup decision share one color: documents with configuration, datasets with model checkpoints, images with audio and video, build output with logs.

**Directories are colored too.** A directory is tinted towards whatever content type accounts for most of its bytes, and the more one-sided the subtree, the stronger the tint; a directory with no clear majority (under half its bytes in one category) stays neutral. That is what makes a fat wedge in the sunburst readable as "all checkpoints" or "all build output" without drilling into it. The tint is computed once per completed scan — a scan still in flight leaves directories neutral, because partial totals would name the wrong winner.

The sunburst legend (bottom-left corner) lists the categories present with each one's share of the scanned bytes, largest first (`■ data 46%`), capped at six entries.

Switch color schemes with `color_theme` in config or the Settings screen (`,`), where a swatch row beside the picker previews the theme and the choice applies as soon as you leave the screen. A theme changes the whole window — header, borders, panels and footer as well as the charts.

There are five:

| Theme | Config key | What it looks like |
| --- | --- | --- |
| Disktide | `disktide` | The default. Warm charcoal, warm gray directories, the six category hues the charts have used since 0.2.24. |
| Cold | `cold` | Deep navy-black. Teal, cyan, blue, periwinkle and orchid categories, with one warm amber kept for `ephemeral` so reclaimable junk still catches the eye in an otherwise cool chart. |
| Colorblind-safe | `colorblind` | Okabe-Ito hues on the same charcoal as Disktide, arranged so the six categories are also a lightness ladder: they stay separable in grayscale, and the diff view's growth/shrink poles move from red/green to orange/blue. |
| Cyberpunk | `cyberpunk` | Near-black indigo under acid green, ultraviolet, neon yellow, hot magenta, electric cyan and laser red. |
| Mono | `mono` | Black and gray only — the charts, and the tree, breadcrumb and progress text with them. The six categories are a gray ladder rather than one flat gray, every step lighter than the directory ring around it, and the diff view becomes one diverging lightness ramp. |

A theme colors the text as well as the charts: directory names, the size bar, warnings and errors all follow it, so `mono` is monochrome everywhere and not just inside the disc.

Every theme's six category colors are measured, not chosen by eye: all fifteen pairs are checked against a protan and deutan colour-vision simulation, against normal vision, and for contrast on that theme's own background. `python tool/gen_palette.py --check` prints the numbers.

Three theme names have been retired and a config naming any of them opens on `disktide`, which is the palette all three described: `warm` (renamed), and `default` — shown as "Neutral" — and `vivid`, both of which measured as duplicates of it.
