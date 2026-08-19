# User Guide

fsmonitor is an interactive terminal tool for exploring disk usage, detecting cleanup opportunities, and tracking how directory sizes change over time.

## Getting Started

Install and launch:

```bash
uv tool install .
fsmonitor
```

The welcome screen shows a single path input with a list of suggested starting directories:

- **Current directory** -- the directory you launched `fsmonitor` from
- **Saved default** -- your previously saved default path (if any)
- **Last visited** -- the most recently explored path (if different from the above)
- **Recent** -- paths from previous `watch` or `scan --snapshot` runs

Press **Up/Down** to cycle through suggestions (the active suggestion is highlighted and fills the input). You can also type any path directly. The right arrow key accepts the ghost-text suggestion; completions update live as you type. Press **Enter** to explore.

Check "Save as default path" to remember the current path as your default for next time. Paths are stored per hostname by default, so they stay relevant when sharing a home directory across servers. Set `hostname_aware_paths = false` under `[ui]` to disable this.

## TUI

The interactive TUI has four modes, switched with the `E`, `C`, `M`, and `F` keys. Cleanup is experimental and disabled by default, so `C` becomes available only after enabling it in Settings.

### Explorer (E)

The main view. A file tree on the left shows directories sorted by the active metric, with inline proportional bars. The right panel shows one of three visualizations:

- **Sunburst** (`1`) -- Concentric rings radiating outward by depth. Each arc's angle represents its share of the parent.
- **Treemap** (`2`) -- Rectangles sized proportionally to disk usage. Drill into directories by clicking or selecting them.
- **Details** (`3`) -- Text panel with metadata about the selected file or directory.

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
| `y` | Copy the highlighted item's absolute path to the clipboard |
| `t` | Cycle Logical, Allocated, Unique, Files across all views |

The indicator line above the tree shows the current sort order and metric. Press `t` to cycle through:

- **Logical** -- apparent payload bytes from `st_size` (default).
- **Allocated** -- `st_blocks * 512` for every visible path, including every hardlink path.
- **Unique** -- allocated payload with each hardlinked inode counted once.
- **Files** -- regular-file and symlink entry count.

The tree, sunburst, treemap, Details rankings, and header change together. Unique hardlink ownership is finalized after the full scan, so a live in-progress view can temporarily show Unique as unavailable. Platforms without `st_blocks` show Allocated/Unique as `Unavailable`; they are never shown as zero. Press `y` to copy the highlighted item's absolute path to the system clipboard; it uses the terminal's OSC 52 escape, so it works over SSH and in web-based shells where there is no local clipboard tool.

Symbolic links are shown as `name → target` and never counted toward folder sizes (only the link's own size). When a link points to a directory, `i` resolves it and rescans from the real location, so linked folders stay navigable without the scan ever traversing the link. Broken links and links to files are marked and cannot be entered.

### Monitor (M)

Shows historical snapshots and size trends. The monitor displays data from the `watch` command or any scans saved with `--snapshot`.

The top half shows a snapshot table and a trend chart. The bottom half shows the largest changes between the two most recent snapshots.

Press `r` to refresh data. The monitor also refreshes automatically each time you switch to it.

By default, path matching is bidirectional: exploring `/data` surfaces watches at `/data/logs`, and exploring `/data/logs` surfaces watches at `/data`. Set `strict_path = true` under `[monitor]` in config to restrict to exact path matches only.

### FS Overview (F)

Shows all mounted real filesystems at a glance. Press `f` to open.

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

Press `Enter` on a filesystem or block-device row to open its details. Press `b` on a filesystem row to run an opt-in throughput probe after a second confirmation. The probe creates a mode-0600 temporary file, writes at most 256 MiB and never more than 25% of currently available space, then removes the file. The displayed read result is approximate because cache eviction is advisory.

Pseudo-filesystems (`proc`, `sysfs`, `tmpfs`, etc.) are automatically filtered out. Press `r` to refresh.

### Cleanup (C) — experimental

Detects pattern-matched candidates such as dependency directories (`node_modules`), build outputs, bytecode files, old logs, OS junk files, and IDE directories. These rules are heuristics, not a guarantee that a path is safe to remove.

Cleanup mode is **disabled by default**. Enable it under "Cleanup Settings" in the Settings screen (`?`). Once enabled, press `c` to switch to it.

Review every selected path before acting. **Delete is permanent**: the current implementation uses direct filesystem deletion and has no trash/quarantine, undo, stale-target revalidation, or persistent audit trail. The confirmation dialog also offers **Dry Run**, which reports what would be deleted without changing the filesystem.

### Key Binding Reference

| Key | Scope | Action |
|-----|-------|--------|
| `e` | Global | Switch to Explorer |
| `m` | Global | Switch to Monitor |
| `f` | Global | Switch to FS Overview |
| `c` | Global | Switch to Cleanup (must be enabled in Settings) |
| `?` | Global | Open settings |
| `q` | Global | Quit (prompts y/n first) |
| `1` / `2` / `3` | Explorer | Sunburst / Treemap / Details |
| `u` / `i` | Explorer | Navigate up / drill into directory |
| `s` | Explorer | Cycle sort order |
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

### watch

Periodic scanning with automatic snapshots. Runs until interrupted or `--max-time` is reached:

```bash
fsmonitor watch /path                   # default interval from config (6h)
fsmonitor watch /path --interval 1h     # scan every hour
fsmonitor watch /path -i 30m -t 12h    # every 30 min, stop after 12 hours
```

Snapshots are saved to the database and visible in the Monitor tab. Old snapshots are pruned automatically based on the `snapshot_retention` setting (default: 30 days).

Since `watch` runs in the foreground, use tmux or nohup for persistent monitoring:

```bash
# tmux (recommended -- reattach later with `tmux attach -t fsmon`)
tmux new -s fsmon
fsmonitor watch /path --interval 6h

# nohup (background, no reattach)
nohup fsmonitor watch /path --interval 6h > /dev/null 2>&1 &
```

For a systemd user service that survives reboots:

```ini
# ~/.config/systemd/user/fsmonitor.service
[Service]
ExecStart=%h/.local/bin/fsmonitor watch /path --interval 6h

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
snapshot_retention = 30                  # days
# max_watch_time = 86400                # optional cap in seconds
# strict_path = true                    # only show exact path matches in Monitor

[ui]
color_theme = "warm"                     # default, cold, warm, vivid, mono
default_viz = "sunburst"                 # treemap, sunburst, details
# show_cleanup = true                    # enable Cleanup mode (disabled by default)
# safe_rendering = true                  # ASCII bars and glyphs for web shells
# live_scan_render = "auto"              # auto | on | off — draw viz live during scan
# default_scan_path = "/home/user/data" # pre-fill welcome screen
# hostname_aware_paths = false           # store paths per hostname (default: true)
```

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
