# User Guide

fsmonitor is an interactive terminal tool for exploring disk usage, detecting cleanup opportunities, and tracking how directory sizes change over time.

## Getting Started

Install and launch:

```bash
uv tool install fsmonitor-cli
fsmonitor
```

For a one-shot launch, use `uvx fsmonitor-cli`. `pipx install
fsmonitor-cli` and a normal `pip install fsmonitor-cli` inside a virtual
environment are also supported. The installed command is always `fsmonitor`;
`fsmonitor-cli` remains a compatibility alias.

The welcome screen shows a single path input with a list of suggested starting directories:

- **Current directory** -- the directory you launched `fsmonitor` from
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

During a scan, the progress panel shows the scan run id, current phase, active
policy, current path, counts, Logical bytes, and rate. Rescan and quit cancel by
run id. Events from an older run are ignored after a newer scan starts, so a
late partial update cannot overwrite the final view from the current run.

Symbolic links are shown as `name → target` and never counted toward folder sizes (only the link's own size). When a link points to a directory, `i` resolves it and rescans from the real location, so linked folders stay navigable without the scan ever traversing the link. Broken links and links to files are marked and cannot be entered.

### Monitor (2)

Monitor Center is the shared setup and management surface for persistent
monitors. Its list/detail layout shows desired state, current host activity,
health, next due time, snapshot count, global database usage, and active alerts.
The detail side has Overview, History, Alerts, and Retention tabs. Terminals
narrower than 90 columns use a list-first view; press **Enter** for details and
**Escape** to return.

| Key | Action |
|-----|--------|
| `n` | Create a monitor; optionally capture the first snapshot now |
| `e` | Edit path, interval, metric, scan policy, workers, or retention preset |
| `p` | Pause or resume the selected definition |
| `R` | Run the selected monitor now |
| `s` | Start or stop this TUI's foreground monitoring session |
| `d` | Archive the monitor after confirmation; history remains |
| `i` | Pin or unpin the selected History snapshot |
| `a` / `A` | Add or edit an alert rule in the Alerts tab |
| `x` / Backspace | Enable/disable or remove the selected alert rule |
| `t` | Run retention maintenance from the Retention tab |
| `r` | Refresh all monitor data |
| `1` | Return to Explorer; lowercase `e` remains Edit |

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
run `fsmonitor watch --monitor/--all` to host scans. Leaving Monitor Center for
Explorer keeps the TUI session alive, while quitting the app stops it and
releases its lease. Wave 06 does not install a daemon.

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

### Cleanup (c) — experimental

Detects pattern-matched candidates such as dependency directories (`node_modules`), build outputs, bytecode files, old logs, OS junk files, and IDE directories. These rules are heuristics, not a guarantee that a path is safe to remove.

Cleanup mode is **disabled by default**. Enable it under "Cleanup Settings" in the Settings screen (`?`). Once enabled, press `c` to switch to it.

Review every selected path before acting. **Delete is permanent**: the current implementation uses direct filesystem deletion and has no trash/quarantine, undo, stale-target revalidation, or persistent audit trail. The confirmation dialog also offers **Dry Run**, which reports what would be deleted without changing the filesystem.

### Key Binding Reference

| Key | Scope | Action |
|-----|-------|--------|
| `1` | Global | Switch to Explorer |
| `2` | Global | Switch to Monitor |
| `3` | Global | Switch to FS Overview |
| `c` | Global | Switch to Cleanup (must be enabled in Settings) |
| `?` | Global | Open settings |
| `q` | Global | Quit (prompts y/n first) |
| `F1` / `F2` / `F3` | Explorer | Sunburst / Treemap / Details |
| `u` / `i` | Explorer | Navigate up / drill into directory |
| `s` | Explorer | Cycle sort order |
| `M` | Explorer | Set up monitoring for the highlighted directory |
| `y` | Explorer | Copy highlighted path to clipboard |
| `t` | Explorer | Cycle Logical / Allocated / Unique / Files across all views |
| Ctrl+U / Ctrl+D | Explorer | Jump tree cursor up / down by a quarter screen |
| Up / Down | Settings | Move focus between fields (also Tab/Shift+Tab) |
| `r` | Explorer, Cleanup, Monitor, FS Overview | Rescan / refresh |
| `b` | FS Overview | Confirm and benchmark the highlighted mount |
| Enter | FS Overview | Open filesystem or block-device details |
| `d` | Cleanup | Delete selected |
| `a` | Cleanup | Select all |
| Space | Cleanup | Toggle row selection |

## CLI Commands

These commands run outside the TUI and print results to stdout.

### doctor

Report the installed version, Python/Textual versions, active platform adapter,
application paths, database status/schema, storage metrics, platform
capabilities, optional extras, and default scan policy:

```bash
fsmonitor doctor
fsmonitor doctor --json
```

The JSON schema is versioned and suitable for attaching to issue reports. App
paths are represented as `~` or `$XDG_*` paths by default; use `--show-paths`
only when raw local paths are intentionally required. The report never walks a
scan tree or lists user files.

### scan

One-shot scan with a text summary:

```bash
fsmonitor scan /path
fsmonitor scan /path --snapshot      # save results to the database
fsmonitor scan /path -d 5 -w 4       # limit depth to 5, use 4 threads
fsmonitor scan /path --metric allocated
fsmonitor scan / --metric unique --one-file-system --exclude-pseudo
```

`--metric` controls the completion total, sorting, and top-directory bars.
Every completion summary still prints Logical, Allocated, and Unique together.
Cross-filesystem scanning is the default; `--one-file-system` leaves visible
`xdev` boundary nodes. Descendant pseudo-filesystem mounts are excluded by
default, while an explicitly selected pseudo root is still scanned. Use
`--include-pseudo` to opt in to descendant pseudo filesystems.

The command prints a short run id, phase, policy, and terminal status. Exit codes
are `0` for complete or partial success, `1` for scan failure, `2` for invalid
input, and `130` for cancellation. A partial result remains usable but includes
an explicit coverage line; cancellation never prints a completion summary.

### compare

Compare a target snapshot with a baseline snapshot:

```bash
fsmonitor compare latest previous /path
fsmonitor compare 42 41
fsmonitor compare --since 7d /path
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
fsmonitor monitor add /data --label data --interval 6h --capture-now
fsmonitor monitor list
fsmonitor monitor status 1
fsmonitor monitor edit 1 --interval 1h --metric allocated
fsmonitor monitor pause 1
fsmonitor monitor resume 1
fsmonitor monitor run 1
fsmonitor monitor retention 1          # preview
fsmonitor monitor retention 1 --apply  # run maintenance
fsmonitor monitor pin 42 --label release
fsmonitor monitor unpin 42
fsmonitor monitor remove 1             # archive; keep history
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
fsmonitor alerts add 1 /data --size 500GiB
fsmonitor alerts add 1 /data/logs --growth 10GiB --window 24h
fsmonitor alerts add 1 /data --percent 20 --cooldown 6h
fsmonitor alerts add 1 /data --free-space 50GiB --severity critical
fsmonitor alerts add 1 /data --inode-free 100000
fsmonitor alerts add 1 /data/incoming --new-large 4GiB
fsmonitor alerts list 1
fsmonitor alerts check 1
fsmonitor alerts disable RULE_ID
fsmonitor alerts enable RULE_ID
fsmonitor alerts remove RULE_ID
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
fsmonitor watch /path                   # transient definition; default 6h
fsmonitor watch /path --interval 1h
fsmonitor watch /path -i 30m -t 12h
fsmonitor watch --monitor 1             # host one saved definition
fsmonitor watch --all                   # host all enabled definitions
```

Each interval is a distinct scan run and prints its run id, policy, terminal
status, duration, and snapshot result. Snapshots are saved to the database and
visible in Monitor Center. `watch PATH` does not silently create a persistent
definition; saved monitors use their stored retention policy, alerts, revision,
and schedule. A repository lease prevents a TUI and CLI host from running the
same monitor concurrently.

Since `watch` runs in the foreground, use tmux or another external supervisor
when it must outlive the current shell:

```bash
# tmux (recommended -- reattach later with `tmux attach -t fsmon`)
tmux new -s fsmon
fsmonitor watch /path --interval 6h

# nohup (background, no reattach; host every enabled saved monitor)
nohup fsmonitor watch --all > /dev/null 2>&1 &
```

An external systemd user unit can supervise the foreground host, but Wave 06
does not install or manage that unit; native daemon/user-service integration is
reserved for a later wave:

```ini
# ~/.config/systemd/user/fsmonitor.service
[Service]
ExecStart=%h/.local/bin/fsmonitor watch --all

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now fsmonitor.service
loginctl enable-linger $USER
```

### cleanup

Interactive CLI cleanup with a confirmation prompt:

```bash
fsmonitor cleanup /path
```

The command summarizes all detected candidates (showing up to five paths per category), then offers to **permanently delete all of them**, including candidates hidden behind an “and more” summary. It does not move items to trash and does not provide undo or revalidation; cancel the prompt unless every detected candidate may be removed.

## Configuration

Settings are stored in `~/.config/fsmonitor-cli/config.toml` (respects `XDG_CONFIG_HOME`). The legacy directory name is retained so existing installations keep their settings. Edit the file directly or use the settings screen (`?` in the TUI).

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

[ui]
color_theme = "warm"                     # default, cold, warm, vivid, mono
default_viz = "sunburst"                 # treemap, sunburst, details
# show_cleanup = true                    # enable Cleanup mode (disabled by default)
# safe_rendering = true                  # ASCII bars and glyphs for web shells
# live_scan_render = "auto"              # auto | on | off — draw viz live during scan
# default_scan_path = "/home/user/data" # pre-fill welcome screen
# hostname_aware_paths = false           # store paths per hostname (default: true)
```

Per-monitor paths, schedules, scan policy, desired state, revisions, retention,
pins, and alerts live only in the SQLite repository. Manage them through
Monitor Center or the `monitor`/`alerts` commands. The `[monitor]` config section
contains global defaults, database budgets, the foreground watch cap, and the
TUI auto-start preference.

The **Live scan rendering** setting (`live_scan_render`) controls whether the active visualization tab redraws as the scan progresses. `auto` (the default) enables it on terminals at least 80 columns by 24 rows with at least 4 CPUs, and stays off on smaller / lower-resource setups where the per-frame redraw cost would compete with the scan. Set to `on` to force it regardless of terminal size, or `off` to wait for the scan to finish and render once.

All fields are optional. Missing values use sensible defaults. When `workers` is omitted, the scanner picks a thread count based on CPU count, system load, filesystem type, and available memory -- detected per scan path, so different directories on different filesystems (e.g., local SSD vs. NFS) automatically use an appropriate thread count.

## File-Type Categories

The treemap and sunburst visualizations color files by category. Each file's extension determines its category, which maps to a hue in the active color scheme.

| Category | Extensions | Typical use |
|----------|-----------|-------------|
| code | py, js, ts, tsx, jsx, vue, c, cpp, h, java, go, rs, rb, sh, css, html, kt, swift, lua, r, php, scala, ipynb | Source code and notebooks |
| document | pdf, doc, docx, odt, tex, txt, md, rst, rtf, epub, ppt, pptx, xls, xlsx | Documents and presentations |
| image | png, jpg, jpeg, gif, svg, bmp, webp, ico, tiff, tif, heic, avif, raw | Image files |
| data | csv, sqlite, db, sql, parquet, npy, npz, h5, hdf5, pkl, pickle, jsonl, arrow, feather, tfrecord, lmdb | Datasets and serialized data |
| model | pt, pth, ckpt, onnx, safetensors, pb, tflite, savedmodel | ML model checkpoints and weights |
| config | json, yaml, yml, toml, xml, ini, cfg, env, conf, properties, lock | Configuration and lock files |
| media | mp3, mp4, wav, avi, mkv, flac, ogg, aac, mov, webm, m4a, m4v | Audio and video |
| archive | zip, tar, gz, bz2, xz, 7z, rar, zst, lz4, deb, rpm, iso | Compressed archives and packages |
| build | o, so, pyc, class, whl, egg, dll, lib, a, obj, jar, war | Compiled artifacts |
| log | log, out, err | Log and output files |
| other | *(everything else)* | Unrecognized extensions |

Files without an extension (e.g., `Makefile`, `Dockerfile`) are classified as "other".

Each of the five built-in color schemes (`default`, `cold`, `warm`, `vivid`, `mono`) assigns a distinct hue to every category. Switch schemes with `color_theme` in config or the Settings screen (`?`). The `mono` scheme uses zero saturation, so all categories appear as shades of gray. The sunburst legend (bottom-left corner) shows which categories are present in the current view.
