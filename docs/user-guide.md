# User Guide

DiskTide is an interactive terminal tool for exploring disk usage, detecting
cleanup opportunities, and tracking how directory sizes change over time.

## Getting Started

Install and launch:

```bash
uv tool install disktide
disktide            # welcome screen
disktide ~/projects # straight into the explorer on that path
```

`uvx disktide`, `pipx install disktide`, and `pip install disktide` also work.
The installed command is always `disktide`; `sizetrail`, `fsmonitor`, and
`fsmonitor-cli` remain compatibility aliases.

The dependency list is unchanged by any of them. On Linux and macOS the
install picks up a prebuilt wheel carrying one small optional C extension
that makes scanning several times faster on multiple workers — nothing to
choose, nothing to add. Anywhere without a prebuilt wheel, pip falls back to
the source distribution, which compiles the same extension if a C compiler
is around and quietly does without it if not. See *Scanner backend* below.

The welcome screen shows a path input with suggested starting directories
(current directory, saved default, last visited, recent scan/watch paths).
Type any path or press Up/Down to cycle through suggestions. Right arrow
accepts ghost-text; Enter explores. Check "Save as default path" to remember
it. Paths are stored per hostname by default (`hostname_aware_paths = false`
to disable).

Naming a directory on the command line (`disktide ~/projects`) skips the
welcome screen and explores that path directly; it is recorded as the last
visited path, so the next bare `disktide` offers it back. A first argument
that is neither a subcommand nor a directory is a usage error (exit 2) that
names both possibilities.

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
| Logical | `st_size` of files and symlinks — apparent payload bytes (default). A directory's own `st_size` is not counted. |
| Allocated | `st_blocks × 512` of every file, symlink **and directory**, the scanned root included — what `du` reports. Every hardlink path counts. |
| Unique | Allocated, but each hardlinked inode counted once. Directories are never duplicates, so their blocks always count. |
| Files | Regular-file and symlink count |

Directories cost storage — 4 KiB apiece on ext4, and on xfs once their names
outgrow the inode — so Allocated and Unique include them. On a tree with
hardlinks `du -s` matches Unique, not Allocated: `du` deduplicates by inode
too.

Platforms without `st_blocks` show Allocated/Unique as "Unavailable," never
zero — and that is the only thing "Unavailable" means. A directory you are
not allowed to read still contributes its own blocks and is reported on the
`Coverage:` line instead, so one unreadable folder cannot blank the totals
for the whole scan.

**Diff mode** — when at least two compatible snapshots exist, `d` switches to
Diff view. Color and glyphs show growth, shrink, new, and removed paths.
Tree rows add a delta and a sparkline. `[`/`]` step through adjacent pairs.

**Symlinks** — shown as `name → target`, never traversed, never counted
toward folder sizes. `i` on a symlinked directory resolves and rescans from
the real location.

**Unreadable entries** — a directory the scan could not open at all is marked
with a red `◐`. Any other directory is marked `◐ N hidden`, where N counts
everything unreadable *at or below* that row — its own entries plus every
descendant's, a denied subdirectory counting as one — so the root's number is
the whole story and not just its own top level. The badge is bright when some
of those entries are directly in that directory and dim when they are all
further down; either way its sizes are lower bounds, shown as `≥`. Details
(`F3`) splits the number ("3 unreadable in this directory · 43 hidden at or
below").

**Progress** — during a scan the progress panel shows phase, worker count,
path counts, rate, and the current path. After 20 seconds it adds a hint:
the worker count it is running with, and that `,` (Settings) → Workers and
then `r` will rescan with a different one. On a shared login node, or after a
worker count was clamped, it says that too.

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
| `F1` / `F2` / `F3` | Explorer | Sunburst / Treemap / Details (browsers claim F1/F3 — Tab to the chart tabs, then `←`/`→`, or use Ctrl+P) |
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

Reports version, Python/Textual versions, terminal geometry, colour depth,
database status, platform capabilities, watch backend, scan policy, and
which scanner backend is live (see *Scanner backend* below).

The Colour block prints `TERM`, `COLORTERM`, `TEXTUAL_COLOR_SYSTEM`, the tmux
clients attached to your session, and the depth that was resolved from them —
plus, when there is colour left on the table, the one line to change.

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

Run status goes to stderr and the report to stdout, so closing stderr
(`disktide scan /path 2>&- > report.txt`) costs the narration and keeps the
report.

A filename is bytes, and not every filename on a filesystem is valid UTF-8.
The text report renders the undecodable ones as their raw bytes -- a
directory named `b"\xff\xfe"` prints as `\xff\xfe/` -- rather than failing
to encode them.

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
# color_depth = "truecolor"              # auto (default) | truecolor | 256 | 16
# safe_rendering = true                  # ASCII-only for web shells
# live_scan_render = "auto"              # auto | on | off
# hostname_aware_paths = false           # default true

[keys]
# preset = "classic"                     # spine (default), safe, classic
# "cleanup.review_plan" = "p"           # override individual keys
```

All fields are optional. When `workers` is omitted, the scanner measures the
path and picks a count: 1 on warm local storage, 2 on a rotational disk, 8 on
a network or FUSE mount, and 16 / 32 / 64 as a sampled mount turns out to
cost 0.5 / 1 / 3 ms per entry. Two things pull that back down: a shared host
that gave this process no CPU allocation (a cluster login node) caps it at 2,
and a 1-minute load above three-quarters of the host's CPUs halves whatever
is left.

An explicit `workers` value skips the measurement but not the host's
ceiling, which is `max(64, 4 × available CPUs)` --- four per CPU because a
scan worker spends most of its life asleep in a `stat`, and never below 64 so
the fastest tier is reachable on a small machine. A larger request is clamped
to the ceiling and a warning says so; it is not an error. `disktide scan`
prints warnings as `Warning:` lines on stderr, `--json` also carries them in
`workers.warnings`, and the TUI shows one notification each. `-w 0` or a
negative value is a usage error (exit 2).

### Faster scans with more cores

```bash
disktide -w 32 /mnt/share          # one run
```

```toml
[scan]
workers = 32                        # every run
```

or Settings (`,`) → Workers, then `r` in the explorer to rescan with it. The
hint beside the box gives both numbers that matter: what the auto policy
would pick here, and the host's ceiling.

More workers help when a scan is *waiting* rather than working. Measured
with `tool/bench_scan.py`, raw mode, native reader, on a 16-CPU node
(2026-09-05; wall seconds):

| Tree | 1w | 4w | 8w | 16w | 32w | 64w |
|------|----|----|----|-----|-----|-----|
| Local xfs, 88k dirs / 888k files, warm | 7.9 | 8.6 | 8.3 | 8.7 | 8.2 | — |
| NFSv4 home, client cache expired | — | — | 16.5 | 14.2 | 13.1 | 12.5 |
| The same NFSv4 tree, client cache warm | — | 9.8 | 9.7 | 10.7 | 10.1 | 9.7 |

On a latency-bound mount, 16 workers takes most of the gain — about 15% over
the auto default of 8 — and 32 to 64 buys another ~10% while system CPU
climbs from 12.7 s to 21 s for it. On warm local disk, or on that same
network tree with the client attribute cache still warm, the count makes no
difference at all: wall clock equals user + sys, so the scan is bound by the
interpreter rather than by the storage, and every count lands inside the
noise of every other. The pure-Python fallback is the one place where more
workers actively hurt — 20.5–23.8 s at 8 and 16 workers on the local tree,
against 7.9 s at one.

Two things that look like worker questions and are not: the first-ever touch
of a cold network tree (131 s at 8 workers here) is the server warming up,
and a shared login node caps at 2 because the cost lands on other people, not
because more would be slower. disktide warns when you override the second.

### Ring shape

The sunburst draws in one of three shapes, cycled with `g`:

| Shape | Description |
|-------|-------------|
| `tiles` (default) | Rectangular rings, straight-line cuts. No calibration needed. |
| `disc` | Circular rings, radial cuts. Needs accurate `cell_aspect`. |
| `fill` | Panel-filling rectangular rings, radial cuts. Needs accurate `cell_aspect`. |

`tiles` looks the same at any cell aspect. Pick `disc` if you want to read
the chart as rays fanning out from the root.

### Scanner backend

Reading a directory means listing it and then asking the size of every entry
in it. Done one entry at a time from Python, each of those questions hands
the interpreter lock back and forth, and on a big tree the handoffs cost more
than the syscalls: eight scanning threads used to finish *slower* than one.
DiskTide ships a small C extension that reads a whole directory and measures
everything in it in one go, which is where most of the speed of a parallel
scan now comes from.

It is optional. Everything works without it; scans on several workers are
just slower.

**Which one am I on?**

```bash
disktide doctor | grep Scanner
#   Scanner: native (_scanfast)
#   Scanner: python fallback (_scanfast is not built for this interpreter ...)
```

`disktide doctor --json` carries the same under `platform.scanner`, and
`tool/bench_scan.py --json` records it next to every timing.

**Turning it off** — `DISKTIDE_ACCEL=0` (also `off`, `no`, `false`) forces
the pure-Python reader for one run:

```bash
DISKTIDE_ACCEL=0 disktide scan /path
```

The two produce identical trees, so this is for comparing speeds or ruling
the extension out of a bug, not for changing what a scan reports.

**If the fallback is what you have.** A wheel for your platform and Python
version carries the extension; a source install builds it when a C compiler
is present. So on a platform PyPI has no wheel for, `pip install disktide`
needs `cc`/`gcc`/`clang` and your Python's development headers
(`python3-dev` / `python3-devel`) to reach the fast path — and it installs
successfully either way, because a missing compiler is not an error.

One thing to know about the fast path: a single directory read is not
interruptible, so cancelling a scan inside one enormous directory (hundreds
of thousands of entries in one place) waits out that read — about a third of
a second. Ordinary directories are microseconds.

### Cell aspect

`disc` and `fill` need to know your terminal's character cell height-to-width
ratio to draw round shapes. DiskTide auto-detects this in most native
terminals (kitty, Alacritty, GNOME Terminal, iTerm2, WezTerm, etc.).

Terminals that report no pixel size — xterm.js web shells, VS Code, Windows
Terminal, mosh, screen — fall back to 2.0, which makes circles slightly oval.
A web shell always lands here and there is nothing to fix in it: node-pty
fills in no pixel winsize, and xterm.js does not answer `CSI 14 t` either, so
every automatic layer comes up empty. Set the value manually:

- **Settings** (`,`) → Cell aspect — type a number or blank for auto-detect.
- **Config** — `cell_aspect` under `[ui]`.
- **Env** — `DISKTIDE_CELL_ASPECT=2.43`.

`disktide doctor` shows which detection mechanism is active.

## Color Themes

Six themes, switched in Settings (`,`) or via `color_theme` in config. A
live swatch previews the choice before you leave the screen.

| Theme | Look |
|-------|------|
| Disktide (default) | Warm charcoal, six category hues |
| Cold | Deep navy, cool teal/cyan/blue palette, amber for ephemeral |
| Colorblind-safe | Okabe-Ito hues, separable in grayscale; diff uses orange/blue instead of red/green |
| Cyberpunk | Near-black indigo, neon accents |
| Mono | Black and gray only, categories as a gray ladder |
| ANSI 16 | The terminal's own sixteen colors, named rather than chosen |

The first five retint the entire window — chrome, tree, and charts — and all
of their category colors are validated for color-vision separation and
contrast. ANSI 16 is different in kind: it names colors instead of specifying
them, so what you see is whatever your terminal's own palette says. Pick it
if you keep a carefully tuned 16-color scheme and want DiskTide to use it;
DiskTide also selects it for itself in a terminal that only has sixteen.

## Web shells and colour depth

DiskTide draws in 24-bit color and the terminal decides what happens to it.
Three common environments answer differently, and two of them start the app
at sixteen colors:

| Environment | `TERM` | What DiskTide does |
|-------------|--------|--------------------|
| Open OnDemand shell | `xterm-16color` | 16 colors → ANSI 16 theme |
| JupyterLab terminal | `xterm-color` | 16 colors → ANSI 16 theme |
| VS Code terminal | `xterm-256color` + `COLORTERM=truecolor` | 24-bit |
| ssh + tmux, local terminal | `tmux-256color` | 256, or 24-bit if the tmux client reports `RGB` |

Both web shells are xterm.js, which can in fact do 256 colors and RGB — it is
the `TERM` their pty is spawned with that says otherwise. Inside tmux the
reading is worse than conservative, it is stale: `TERM` describes the pty tmux
handed you, not the client on the other end, so an app that trusts it writes
256-color codes that tmux then quantises through a fixed table before the
browser sees them. On the default palette that takes 39 distinct colors down
to 13, which is why `archive`, `ephemeral` and the directory rings all came
out the same pink.

So DiskTide asks tmux which clients are attached and takes the least capable
one, because a session you read from a laptop and a browser at once has to be
legible in the browser. At sixteen colors it renders with the ANSI 16 theme,
which is designed for that depth rather than crushed onto it, and says so once
in a toast.

Inside tmux that answer also outranks `COLORTERM`. `COLORTERM` describes a
process's environment, not a pane: under tmux it is whatever the shell that
started the server exported, it survives every detach and reattach, and it
says nothing about the client currently reading the session. Setting it in
your rc and then opening the same session in a web shell would otherwise
report 24-bit in the browser — which is exactly the case that produced the
pink-and-purple screenshot.

To override:

- **Config** — `color_depth` under `[ui]`: `auto` (default), `truecolor`, `256`, `16`.
- **Env** — `DISKTIDE_COLOR_DEPTH=truecolor`, for one session.

Both of those outrank everything below, tmux included. To fix the detection
itself rather than override it:

- **Under tmux** — tell tmux your client is better than its terminfo entry.
  This is the only line that matters in a multiplexed session:

  ```tmux
  set -as terminal-features ",xterm-16color:256,RGB"
  ```

  then detach and reattach (or `tmux kill-server`). `tmux list-clients -F
  '#{client_termname} #{client_termfeatures}'` shows what to name.

- **Not under tmux** — `export COLORTERM=truecolor` in the shell the app runs
  in. Every xterm.js-based shell and every modern terminal accepts RGB
  whatever its `TERM` says, and `COLORTERM` is not forwarded by ssh, so this
  is worth setting in a native terminal over ssh too. DiskTide reads it
  outside tmux, and inside tmux only when it could not ask tmux at all.

`disktide doctor` prints the whole diagnosis: `TERM`, `COLORTERM`,
`TEXTUAL_COLOR_SYSTEM`, the tmux clients attached to your session with their
feature lists, the depth that was resolved and which of those decided it, and
the one line to change when there is color left on the table.

### Glyphs in a browser terminal

The other half of a web shell is the font. xterm.js defaults to `courier-new,
courier, monospace`, and Courier New fails the block elements (`▀▄█▌▐░▒▓
▁▂▃▅▆▇ ▏▎▍▋▊▉ ▔▕ ▖▗▘▝▛▜▙▟`) in two different ways that end the same place.

The eighth blocks and the quadrants it does not have at all, so the browser
falls back to a proportional face and draws them at *that* font's width. A
border row built from them comes out 1.2–1.8× wider than the box it belongs
to: a 72-cell input drew its `▔` row 129 cells wide and its `▁` row 86.

The eight it does have — the halves, the full block, the shades — it draws
with ink that is not fitted to a terminal cell. Measured on an explorer at
307×71: every `░` about 1.1 cells wide and 1.4 rows tall, so one row's bar
bled over the size text above and below it, and `▀`/`▄` *narrower* than the
cell, leaving a comb of background-coloured slits along every horizontal edge
of the chart.

So DiskTide draws no block element anywhere. **Every fill is a background
colour on spaces** — the tree's proportional bar, the FS Overview's usage and
capacity bars, the scan progress bar, the theme swatches in Settings, the
sunburst and the treemap — and anything that needs a *ramp* rather than a fill
is ASCII: the mini trend sparkline is `.:-=+*#`, and the `disktide scan`
summary bar on stdout is `#` and `-`. The Monitor's trend chart plots its line
with `•` (one dot per cell) rather than plotext's default half-and-quadrant
markers. Box drawing (`─│├└┼━╔═╗`), the tree's `▶`/`▼`, the legend's `■`, and
`●○` were measured at one cell and stay.

Two consequences worth knowing:

- **Use the `tiles` ring shape in a browser** — it is the default. Every edge
  it draws lands on a cell edge, so every cell of the chart is one flat colour.
  `disc` and `fill` are round, and a round edge has to be anti-aliased with
  `▀`/`▄`, which is exactly what the browser cannot fit. Press `g` to cycle
  shapes, or set `ring_shape` under `[ui]`.
- **You can also give the shell a better font.** Open OnDemand exposes no font
  setting, but JupyterLab and VS Code do (`terminal.integrated.fontFamily`),
  and any face with real box-drawing coverage — DejaVu Sans Mono, Cascadia
  Mono, Menlo — renders everything at one cell.

`tool/capture_glyphs.py` re-checks this against real panes at 307×71 and
120×32, and `tests/test_web_glyphs.py` gates it in CI.

`ui.safe_rendering` is a different switch, and after all of the above it is
only for a terminal whose font stops at ASCII or that cannot be trusted with
background colours at all: it draws the tree's bar with `#`, the theme
swatches with `##`, and the access markers as `[!]`/`[~]`.

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
