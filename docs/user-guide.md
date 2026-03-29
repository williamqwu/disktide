# User Guide

fsmonitor-cli is an interactive terminal tool for exploring disk usage, detecting cleanup opportunities, and tracking how directory sizes change over time.

## Getting Started

Install and launch:

```bash
pip install -e .
fsmonitor-cli
```

The welcome screen shows a single path input with a list of suggested starting directories:

- **Current directory** -- the directory you launched `fsmonitor-cli` from
- **Saved default** -- your previously saved default path (if any)
- **Last visited** -- the most recently explored path (if different from the above)
- **Recent** -- paths from previous `watch` or `scan --snapshot` runs

Press **Up/Down** to cycle through suggestions (the active suggestion is highlighted and fills the input). You can also type any path directly. The right arrow key accepts the ghost-text suggestion; completions update live as you type. Press **Enter** to explore.

Check "Save as default path" to remember the current path as your default for next time. Paths are stored per hostname by default, so they stay relevant when sharing a home directory across servers. Set `hostname_aware_paths = false` under `[ui]` to disable this.

## TUI

The interactive TUI has three modes, switched with the `E`, `M`, and `F` keys.

### Explorer (E)

The main view. A file tree on the left shows directories sorted by size, with inline size bars. The right panel shows one of three visualizations:

- **Treemap** (`1`) -- Rectangles sized proportionally to disk usage. Drill into directories by clicking or selecting them.
- **Sunburst** (`2`) -- Concentric rings radiating outward by depth. Each arc's angle represents its share of the parent.
- **Details** (`3`) -- Text panel with metadata about the selected file or directory.

Navigation:

| Key | Action |
|-----|--------|
| Up/Down | Move through the tree |
| Left/Right | Collapse/expand tree nodes |
| `u` | Go up to the parent directory |
| `i` | Drill into the selected directory (rescans from there) |
| `s` | Cycle sort order: size, name, modified |
| `r` | Rescan the current directory |

### Monitor (M)

Shows historical snapshots and size trends. The monitor displays data from the `watch` command or any scans saved with `--snapshot`.

The top half shows a snapshot table and a trend chart. The bottom half shows the largest changes between the two most recent snapshots.

Press `r` to refresh data. The monitor also refreshes automatically each time you switch to it.

By default, path matching is bidirectional: exploring `/data` surfaces watches at `/data/logs`, and exploring `/data/logs` surfaces watches at `/data`. Set `strict_path = true` under `[monitor]` in config to restrict to exact path matches only.

### FS Overview (F)

Shows all mounted real filesystems at a glance. Press `f` to open.

The top bar summarises total mounted space and usage percentage, with a proportional coloured bar showing each filesystem's relative size. The table lists each filesystem with:

| Column | Description |
|--------|-------------|
| Mount | Mountpoint path |
| FS Type | Filesystem type (ext4, xfs, nfs4, etc.) |
| Speed | Speed tier based on storage class |
| Total / Used / Free | Disk space |
| Usage | Visual bar + percentage |

**Speed tiers:**

| Tier | Colour | When |
|------|--------|------|
| Fast (SSD) | Green | Non-rotational local storage |
| Medium (HDD) | Yellow | Rotational local storage |
| Slow (Network) | Red | NFS, CIFS, SSHFS, etc. |
| Unknown | Dim | Could not detect storage type |

Pseudo-filesystems (`proc`, `sysfs`, `tmpfs` with no blocks, etc.) are automatically filtered out. Press `r` to refresh.

### Cleanup (C) — experimental

Detects files and directories that can safely be removed: dependency caches (`node_modules`), build outputs, bytecode files, old logs, OS junk files, and IDE caches.

Cleanup mode is **disabled by default**. Enable it under "Cleanup Settings" in the Settings screen (`?`). Once enabled, press `c` to switch to it.

### Key Binding Reference

| Key | Scope | Action |
|-----|-------|--------|
| `e` | Global | Switch to Explorer |
| `m` | Global | Switch to Monitor |
| `f` | Global | Switch to FS Overview |
| `c` | Global | Switch to Cleanup (must be enabled in Settings) |
| `?` | Global | Open settings |
| `q` | Global | Quit |
| `1` / `2` / `3` | Explorer | Treemap / Sunburst / Details |
| `u` / `i` | Explorer | Navigate up / drill into directory |
| `s` | Explorer | Cycle sort order |
| `r` | Explorer, Cleanup, Monitor, FS Overview | Rescan / refresh |
| `d` | Cleanup | Delete selected |
| `a` | Cleanup | Select all |
| Space | Cleanup | Toggle row selection |

## CLI Commands

These commands run outside the TUI and print results to stdout.

### scan

One-shot scan with a text summary:

```bash
fsmonitor-cli scan /path
fsmonitor-cli scan /path --snapshot      # save results to the database
fsmonitor-cli scan /path -d 5 -w 4       # limit depth to 5, use 4 threads
```

### watch

Periodic scanning with automatic snapshots. Runs until interrupted or `--max-time` is reached:

```bash
fsmonitor-cli watch /path                   # default interval from config (6h)
fsmonitor-cli watch /path --interval 1h     # scan every hour
fsmonitor-cli watch /path -i 30m -t 12h    # every 30 min, stop after 12 hours
```

Snapshots are saved to the database and visible in the Monitor tab. Old snapshots are pruned automatically based on the `snapshot_retention` setting (default: 30 days).

Since `watch` runs in the foreground, use tmux or nohup for persistent monitoring:

```bash
# tmux (recommended -- reattach later with `tmux attach -t fsmon`)
tmux new -s fsmon
fsmonitor-cli watch /path --interval 6h

# nohup (background, no reattach)
nohup fsmonitor-cli watch /path --interval 6h > /dev/null 2>&1 &
```

For a systemd user service that survives reboots:

```ini
# ~/.config/systemd/user/fsmonitor.service
[Service]
ExecStart=%h/.local/bin/fsmonitor-cli watch /path --interval 6h

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now fsmonitor.service
loginctl enable-linger $USER
```

### cleanup

Non-interactive cleanup with confirmation prompt:

```bash
fsmonitor-cli cleanup /path
```

## Configuration

Settings are stored in `~/.config/fsmonitor-cli/config.toml` (respects `XDG_CONFIG_HOME`). Edit the file directly or use the settings screen (`?` in the TUI).

```toml
[scan]
max_depth = 10                           # omit for unlimited
workers = 4                              # omit for auto-detect
follow_symlinks = false
exclude_patterns = [".git", "node_modules"]

[cleanup]
require_confirm_dangerous = true
# enabled_rules = ["node_modules", "old_logs"]   # only run these rules
# disabled_rules = ["ide_caches"]                # skip these rules

[monitor]
default_interval = 21600                 # 6 hours, in seconds
snapshot_retention = 30                  # days
# max_watch_time = 86400                # optional cap in seconds
# strict_path = true                    # only show exact path matches in Monitor

[ui]
color_theme = "warm"                     # default, cold, warm, vivid, mono
default_sort = "size"                    # size, name, mtime
default_viz = "treemap"                  # treemap, sunburst, details
show_hidden = false
# show_cleanup = true                    # enable Cleanup mode (disabled by default)
# default_scan_path = "/home/user/data" # pre-fill welcome screen
# hostname_aware_paths = false           # store paths per hostname (default: true)
```

All fields are optional. Missing values use sensible defaults. When `workers` is omitted, the scanner picks a thread count based on CPU count, system load, filesystem type, and available memory — detected per scan path, so different directories on different filesystems (e.g., local SSD vs. NFS) automatically use an appropriate thread count.
