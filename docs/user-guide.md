# User Guide

DiskTide is an interactive terminal tool for exploring disk usage, detecting
cleanup opportunities, and tracking how directory sizes change over time.

## Getting Started

Install and launch:

```bash
uv tool install disktide
disktide
```

`uvx disktide`, `pipx install disktide`, and `pip install disktide` also work.
The installed command is always `disktide`; `sizetrail`, `fsmonitor`, and
`fsmonitor-cli` remain compatibility aliases.

The welcome screen shows a path input with suggested starting directories
(current directory, saved default, last visited, recent scan/watch paths).
Type any path or press Up/Down to cycle through suggestions. Right arrow
accepts ghost-text; Enter explores. Check "Save as default path" to remember
it. Paths are stored per hostname by default (`hostname_aware_paths = false`
to disable).

## TUI

The TUI uses `1`, `2`, `3` for Explorer, Monitor, and FS Overview; `c` for
Cleanup (disabled by default). Press `?` for the key map and `,` for Settings.

### Explorer (1)

A file tree on the left, sorted by the active metric, with one of three
visualizations on the right:

- **Sunburst** (`F1`) — concentric rings by depth, arc angle = share of parent.
- **Treemap** (`F2`) — rectangles proportional to size.
- **Details** (`F3`) — text metadata for the selected item.

| Key | Action |
|-----|--------|
| Up / Down | Move through the tree |
| Ctrl+U / Ctrl+D | Jump up / down by a quarter of the visible tree |
| Left / Right | Collapse / expand tree nodes |
| `u` | Go up to the parent directory |
| `i` | Drill into the selected directory (rescans symlinked directories) |
| `s` | Cycle sort order: size, name, modified |
| `r` | Rescan (prompts y/n) |
| `d` | Toggle Current / Diff view |
| `[` / `]` | Browse newer / older snapshot pairs |
| `M` | Set up monitoring for the highlighted directory |
| `y` | Copy highlighted path to clipboard (works over SSH via OSC 52) |
| `t` | Cycle metric: Logical, Allocated, Unique, Files |
| `g` | Cycle sunburst shape: tiles / disc / fill |

**Size metrics** — `t` switches the metric across all views at once:

| Metric | Measures |
|--------|----------|
| Logical | `st_size` — apparent payload bytes (default) |
| Allocated | `st_blocks × 512` — includes every hardlink path |
| Unique | Allocated, but each hardlinked inode counted once |
| Files | Regular-file and symlink count |

Platforms without `st_blocks` show Allocated/Unique as "Unavailable," never
zero.

**Diff mode** — when at least two compatible snapshots exist, `d` switches to
Diff view. Color and glyphs show growth, shrink, new, and removed paths.
Tree rows add a delta and a sparkline. `[`/`]` step through adjacent pairs.

**Symlinks** — shown as `name → target`, never traversed, never counted
toward folder sizes. `i` on a symlinked directory resolves and rescans from
the real location.

**Progress** — during a scan the progress panel shows phase, worker count,
path counts, rate, and the current path.

### Monitor (2)

Monitor Center manages persistent monitors — saved definitions that track
how a directory's size changes over time.

| Key | Action |
|-----|--------|
| `n` | Create a monitor (optionally capture first snapshot now) |
| `e` | Edit path, interval, metric, scan policy, workers, or retention |
| `p` | Pause / resume |
| `R` | Run selected monitor now |
| `g` | Run a trusted full reconciliation |
| `S` | Start / stop continuous sampling |
| `i` | Pin / unpin a snapshot |
| `a` / `A` | Add / edit an alert rule |
| `x` / Backspace | Enable/disable or remove alert rule |
| `t` | Run retention maintenance |
| `r` | Refresh all monitor data |
| Tab | Cycle history charts (F1–F4) |

**Auto-start**: the toggle at the top persists `monitor.auto_start_in_tui`
so future TUI launches start the foreground host automatically. It does not
install a daemon.

**History** has four views, all sharing one baseline/target snapshot pair
(set with `b`/`v` on a snapshot; `l` restores latest/previous):

| View | What it shows |
|------|---------------|
| Trend (`F1`) | Line chart of root and selected subtree over time |
| Diff Map (`F2`) | Treemap comparing baseline vs. target |
| Growth Rings (`F3`) | Sunburst with a growth overlay |
| Heatmap (`F4`) | Paths ranked by persistent growth across intervals |

**No daemon**: scans run only while the TUI session or a `disktide watch`
foreground host is alive. Monitors show `enabled · no-host` when no process
owns them — press `S` or run `disktide watch --all` to host.

### FS Overview (3)

Shows all mounted real filesystems: mount path, type, storage medium (Flash /
HDD / RAM / Network), capacity, usage bar, and user quota when available.
Block devices and partitions appear in a second panel when `lsblk` is
available.

| Key | Action |
|-----|--------|
| Enter | Open filesystem or block-device details |
| `B` | Benchmark the highlighted mount (writes a temp file after confirmation) |
| `r` | Refresh |

Pseudo-filesystems are filtered out. Unavailable probes show their reason
instead of an empty panel.

### Cleanup (c)

Detects reclaimable artifacts through versioned rule packs for Python, Node,
Rust, general logs/temp, IDE metadata, and container caches.

**Disabled by default.** Enable it in Settings (`,`) under "Cleanup Settings,"
then press `c`.

| Key | Action |
|-----|--------|
| Space / `a` | Toggle / select all rows |
| `p` | Create and review a CleanupPlan |
| `z` | Undo the latest recoverable plan |
| `h` | Show savings history by category |
| `m` | Focus the synchronized Age/Size Map |

**Workflow**: select candidates → `p` to review a plan → **Apply Safely**
moves targets to system Trash (or owned quarantine if Trash is unavailable)
→ `z` to undo if needed. Permanent deletion is a separate red action
requiring a typed `DELETE <plan-id>` confirmation.

Every target is re-checked (identity, age, rule match, mount boundary,
protected paths) before any action. Changed or missing targets are skipped.

### Mouse

On by default. Disable with `--no-mouse` or `mouse = false` under `[ui]`.

| Gesture | Action |
|---------|--------|
| Click tree row | Select (directory drills in) |
| Click sunburst arc / treemap rectangle | Select that path |
| Click sunburst centre | Go up one level |
| Wheel | Scroll tree and Details panel |
| Hover chart shape | Tooltip: name, size, share |
| Shift+drag | Terminal's own text selection |

### Key Binding Reference

Press `?` in the app for a live, screen-specific version of this table.
`Ctrl+P` searches every action by name.

| Key | Scope | Action |
|-----|-------|--------|
| `1` `2` `3` `c` | Global | Explorer / Monitor / FS Overview / Cleanup |
| `?` | Global | Key map for the current screen |
| `,` | Global | Settings |
| Ctrl+P | Global | Command palette |
| `q` | Global | Quit |
| `r` | Explorer, Cleanup, Monitor, FS Overview | Rescan / refresh |
| Esc | Modals, Monitor detail | Back / close |
| `F1` / `F2` / `F3` | Explorer | Sunburst / Treemap / Details |
| `u` / `i` | Explorer | Up / drill into directory |
| `s` | Explorer | Cycle sort order |
| `d` | Explorer | Toggle Current / Diff |
| `t` | Explorer | Cycle metric |
| `y` | Explorer | Copy path to clipboard |
| `M` | Explorer | Set up monitoring |
| `[` / `]` | Explorer | Older / newer snapshot pair |
| `g` | Explorer | Cycle ring shape |
| Ctrl+U / Ctrl+D | Explorer | Jump tree cursor ¼ screen |
| `n` / `e` / `p` | Monitor | New / edit / pause-resume |
| `R` | Monitor | Run now |
| `S` | Monitor | Start / stop sampling |
| `g` | Monitor | Full reconciliation |
| `b` / `v` | Monitor | Set baseline / target snapshot |
| Tab | Monitor | Cycle history charts |
| `B` | FS Overview | Benchmark mount |
| Enter | FS Overview | Open details |
| Space / `a` | Cleanup | Toggle / select all |
| `p` | Cleanup | Create and review plan |
| `z` | Cleanup | Undo latest plan |
| `h` | Cleanup | Savings history |
| `m` | Cleanup | Focus Age/Size Map |

### Remapping keys

Every binding has an id. Override in `config.toml`:

```toml
[keys]
preset = "classic"          # start from the old layout
"cleanup.review_plan" = "p" # override one key on top
```

Three presets:

| Preset | Description |
|--------|-------------|
| `spine` | Current default layout |
| `safe` | v0.2.30 layout with three consequence mismatches fixed (`d`, `u`, `s`) |
| `classic` | v0.2.30 layout exactly |

The key map (`?`) always reflects your bindings. An id that no longer exists
costs that one binding and a warning toast, not the session.

## CLI Commands

Results go to stdout, run status to stderr. `--help` works at every level.
`--json` writes one parseable document to stdout.

### doctor

```bash
disktide doctor            # human-readable
disktide doctor --json     # versioned JSON, suitable for issue reports
```

Reports version, Python/Textual versions, terminal geometry, database status,
platform capabilities, watch backend, and scan policy.

### scan

```bash
disktide scan /path
disktide scan /path --snapshot          # save to database
disktide scan /path -d 5 -w 4          # depth 5, 4 workers
disktide scan /path --metric allocated
disktide scan /path --one-file-system --exclude-pseudo
disktide scan /path --json
```

`--metric` controls sorting and the top-directory bars; the summary always
prints all three byte totals. Exit codes: `0` success/partial, `1` failure,
`2` invalid input, `130` cancelled.

### compare

```bash
disktide compare latest previous /path
disktide compare 42 41
disktide compare --since 7d /path
```

First selector is target, second is baseline (reads `baseline → target`).
Selectors: `latest`, `previous`, `oldest`, or a numeric snapshot id. Policy
must be compatible; incompatible comparisons exit with status 2. `--raw`
forces an untrusted diagnostic diff.

### monitor

```bash
disktide monitor add /data --label data --interval 6h --capture-now
disktide monitor list
disktide monitor status 1
disktide monitor edit 1 --interval 1h --metric allocated
disktide monitor pause 1
disktide monitor resume 1
disktide monitor run 1
disktide monitor reconcile 1
disktide monitor retention 1             # preview
disktide monitor retention 1 --apply     # run maintenance
disktide monitor pin 42 --label release
disktide monitor unpin 42
disktide monitor remove 1                # archive; keep history
```

Identifiers can be numeric ids or labels. `list/status --json` for scripting.

### alerts

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

`alerts check` exits `0` (nothing), `2` (unsuppressed trigger), `3`
(all suppressed).

### watch

Run the foreground monitor host until interrupted or `--max-time`:

```bash
disktide watch /path                     # transient, default 6h
disktide watch /path --interval 1h
disktide watch /path --events            # require disktide[watch]
disktide watch /path --periodic-only     # no optional dependency
disktide watch --monitor 1               # host one saved definition
disktide watch --all                     # host all enabled definitions
```

`auto` mode (default) uses the native backend when installed, otherwise falls
back to periodic. `--events` is strict and exits if the backend is missing.

For long-running hosting, use tmux or a systemd user unit:

```bash
tmux new -s fsmon
disktide watch --all --interval 6h
```

A copyable systemd example is in `docs/examples/disktide-watch.service`.

### cleanup

`disktide cleanup PATH` is shorthand for `disktide cleanup plan PATH`.

```bash
disktide cleanup /path                              # preview (read-only)
disktide cleanup --plan PLAN_ID --apply              # safe apply
disktide cleanup --plan PLAN_ID --permanent          # requires DELETE <plan-id>
disktide cleanup history --by category
disktide cleanup undo PLAN_OR_ACTION_ID
disktide cleanup purge PLAN_OR_ACTION_ID             # requires PURGE <plan-id>
disktide cleanup quarantine audit /path/.disktide-quarantine
disktide cleanup quarantine rebuild /path/.disktide-quarantine
disktide cleanup rules list
disktide cleanup rules validate /path/to/pack.toml
disktide cleanup rules disable node
disktide cleanup rules enable node
```

The first command scans and saves a plan but makes no filesystem changes.
`--apply` revalidates each target and uses Trash with quarantine fallback.
`--permanent` is irreversible and requires a typed confirmation.

Built-in rule packs load from the package. User packs from
`~/.config/disktide/cleanup-rules/*.toml`.

## Configuration

Edit `~/.config/disktide/config.toml` directly or press `,` in the TUI.
Respects `XDG_CONFIG_HOME`. Legacy `sizetrail`/`fsmonitor-cli` configs are
picked up automatically.

```toml
[scan]
max_depth = 10                           # omit for unlimited
workers = 4                              # omit for auto-detect
# one_file_system = true                 # default: false
# exclude_pseudo_filesystems = false     # default: true

[monitor]
default_interval = 21600                 # 6 hours, in seconds
database_soft_budget = 2147483648        # 2 GiB
database_hard_budget = 3221225472        # 3 GiB
# auto_start_in_tui = true              # default false
event_mode = "auto"                      # auto | events | periodic

[cleanup]
# prefer_trash = false                   # default true
quarantine_retention_days = 7
quarantine_max_bytes = 10737418240       # 10 GiB
# disabled_rule_packs = ["node"]

[ui]
color_theme = "disktide"                 # disktide, cold, colorblind, cyberpunk, mono
default_viz = "sunburst"                 # treemap, sunburst, details
# show_cleanup = true                    # default false
# ring_shape = "disc"                    # default tiles
# mouse = false                          # default true
# cell_aspect = 2.43                     # omit to auto-detect
# safe_rendering = true                  # ASCII-only for web shells
# live_scan_render = "auto"              # auto | on | off
# hostname_aware_paths = false           # default true

[keys]
# preset = "classic"                     # spine (default), safe, classic
# "cleanup.review_plan" = "p"           # override individual keys
```

All fields are optional. When `workers` is omitted, the scanner picks a
count based on filesystem type and storage medium (1 for local SSD, up to 4
for network mounts). Explicit values are used exactly.

### Ring shape

The sunburst draws in one of three shapes, cycled with `g`:

| Shape | Description |
|-------|-------------|
| `tiles` (default) | Rectangular rings, straight-line cuts. No calibration needed. |
| `disc` | Circular rings, radial cuts. Needs accurate `cell_aspect`. |
| `fill` | Panel-filling rectangular rings, radial cuts. Needs accurate `cell_aspect`. |

`tiles` looks the same at any cell aspect. Pick `disc` if you want to read
the chart as rays fanning out from the root.

### Cell aspect

`disc` and `fill` need to know your terminal's character cell height-to-width
ratio to draw round shapes. DiskTide auto-detects this in most native
terminals (kitty, Alacritty, GNOME Terminal, iTerm2, WezTerm, etc.).

Terminals that report no pixel size — xterm.js web shells, VS Code, Windows
Terminal, mosh, screen — fall back to 2.0, which makes circles slightly oval.
Set the value manually:

- **Settings** (`,`) → Cell aspect — type a number or blank for auto-detect.
- **Config** — `cell_aspect` under `[ui]`.
- **Env** — `DISKTIDE_CELL_ASPECT=2.43`.

`disktide doctor` shows which detection mechanism is active.

## Color Themes

Five themes, switched in Settings (`,`) or via `color_theme` in config. A
live swatch previews the choice before you leave the screen.

| Theme | Look |
|-------|------|
| Disktide (default) | Warm charcoal, six category hues |
| Cold | Deep navy, cool teal/cyan/blue palette, amber for ephemeral |
| Colorblind-safe | Okabe-Ito hues, separable in grayscale; diff uses orange/blue instead of red/green |
| Cyberpunk | Near-black indigo, neon accents |
| Mono | Black and gray only, categories as a gray ladder |

Each theme retints the entire window — chrome, tree, and charts. All category
colors are validated for color-vision separation and contrast.

## File-Type Categories

Extensions determine category; six categories carry a color, unrecognized
extensions are neutral gray.

| Category | Typical extensions |
|----------|--------------------|
| code | py, js, ts, c, cpp, go, rs, rb, sh, css, html, … |
| docs | pdf, doc, md, txt, json, yaml, toml, xml, lock, … |
| data | csv, sqlite, parquet, h5, pkl, safetensors, … |
| media | png, jpg, mp4, wav, svg, gif, … |
| archive | zip, tar, gz, 7z, rar, zst, deb, iso, … |
| ephemeral | pyc, so, whl, dll, log, class, o, obj, … |

Everything inside a known regenerable container (`.venv`, `node_modules`,
`__pycache__`, etc.) counts as ephemeral wholesale.

Directories are tinted toward their dominant content category. A directory
with no clear majority stays neutral. The sunburst legend shows each
category's share.
