# fsmonitor

Interactive terminal disk usage explorer built with Python and [Textual](https://github.com/Textualize/textual).

Explore the same tree by logical bytes, allocated blocks, unique on-disk
allocation, or file count. Sparse files, hardlinks, filesystem boundaries,
policy exclusions, and unreadable subtrees stay explicit instead of being
collapsed into one ambiguous size number.

<p align="center">
  <img src="docs/images/sunburst.png" width="49%" alt="Explorer with sunburst visualization" />
  <img src="docs/images/monitor.png" width="49%" alt="Monitor with snapshot history and size trends" />
</p>

## Installation

Run once without installing:

```bash
uvx fsmonitor-cli
```

Install as an isolated command with any supported tool:

```bash
uv tool install fsmonitor-cli
pipx install fsmonitor-cli

# Standard virtual environment
python -m venv .venv
. .venv/bin/activate
python -m pip install fsmonitor-cli
```

Upgrade or uninstall:

```bash
uv tool upgrade fsmonitor-cli
uv tool uninstall fsmonitor-cli
pipx upgrade fsmonitor-cli
pipx uninstall fsmonitor-cli
```

The Python distribution is still named `fsmonitor-cli` for package-index compatibility. The canonical command is `fsmonitor`; the previous `fsmonitor-cli` command remains available as a compatibility alias.

The core wheel is pure Python and has no compiler requirement. Development
checkouts use `uv sync --locked`; see [Contributing](docs/contributing.md).

**Stored data.** The app follows XDG conventions and writes below two persistent roots. Deleting either root is safe for the filesystem, but resets the corresponding settings, custom cleanup policy, or snapshot history. The existing `fsmonitor-cli` directory names are intentionally retained so upgrades keep current data:

| Location | Contents | Typical size |
|----------|----------|--------------|
| `~/.config/fsmonitor-cli/config.toml` | User settings | < 1 KB |
| `~/.config/fsmonitor-cli/cleanup-rules/*.toml` | Optional schema-v1 declarative cleanup rule packs | User-defined |
| `~/.local/share/fsmonitor-cli/data.db` | SQLite monitor definitions, snapshots/deltas, retention/alert history, and CleanupPlan/audit records | Depends on tree size and snapshot count |

Snapshot data is written by `scan --snapshot`, explicit monitor runs, and active
foreground monitor hosts. `fsmonitor compare` checks policy and root
compatibility before reporting growth. Monitor Center in the TUI can create,
edit, pause, run, archive, pin, and manage alerts for the same persistent
definitions exposed by the CLI. Its History tab shares one space-time
vocabulary across Trend, Diff Treemap, growth-overlay Sunburst, and
persistent-growth Heatmap views. Definitions do not install a daemon: scans run
only while the current TUI monitoring session or `fsmonitor watch` foreground
host is active. Versioned retention policies roll older history into time
buckets, preserve pinned snapshots, and enforce configurable database budgets.

Paths respect `XDG_CONFIG_HOME` and `XDG_DATA_HOME` if set. Settings can also be edited by pressing `?` inside the TUI. See the [User Guide](docs/user-guide.md) for the full configuration reference.

## Quick Start

`fsmonitor` has two modes of operation: an **interactive TUI** for visual exploration (the main interface), and CLI commands for diagnostics, scripting, monitoring, and cleanup.

### TUI

```bash
# Launch interactive TUI (opens welcome screen)
fsmonitor
```

#### Key Bindings

| Key | Action |
|-----|--------|
| `1` / `2` / `3` | Switch mode (Explorer / Monitor / FS Overview) |
| `c` | Switch to Cleanup mode (enable it in Settings first) |
| `F1` / `F2` / `F3` | Switch visualization (Sunburst / Treemap / Details) |
| `d` / `[` / `]` | Toggle Current/Diff / browse adjacent snapshot pairs |
| `u` / `i` | Navigate up / drill into directory |
| `s` | Cycle sort (Size / Name / Modified) |
| `M` | Set up monitoring for the highlighted Explorer directory |
| `y` / `t` | Copy highlighted path / cycle Logical, Allocated, Unique, Files |
| `r` | Rescan / refresh |
| `b` | Benchmark the highlighted mount in FS Overview (after confirmation) |
| `?` | Settings |
| `q` | Quit |

Inside Monitor Center, use `n` to create a monitor, `e` to edit it, `p` to
pause/resume, `R` to run now, and `s` to start/stop the current TUI host. The
History tab uses `F1`–`F4` for Trend, Diff Map, Growth Rings, and Heatmap; `b`
and `v` mark the highlighted snapshot as baseline/target, while `l` restores
latest/previous. The footer keeps the mode hint visible there; press `1` to
return to Explorer without conflicting with lowercase `e` for Edit.

Cleanup is disabled by default in the TUI. After enabling it in Settings, press
`c` to review candidates from versioned Python, Node, Rust, general, IDE, and
container rule packs. The Age/Size Map ranks opportunities without treating a
score as proof of safety; press `m` to focus it and `h` for grouped savings
history. Select candidates and press `d` to review a persisted CleanupPlan.
Normal apply uses system Trash when an atomic same-filesystem move is available
and otherwise uses an owned quarantine directory; `u` restores the latest
recoverable plan. Detection-only provider caches cannot be applied through the
generic executor. Permanent deletion is a separate typed-confirmation path.

### CLI Commands

```bash
# Explain this installation and its available platform features
fsmonitor doctor
fsmonitor doctor --json

# Scan a directory directly
fsmonitor scan /path --metric allocated --snapshot

# Stay on one filesystem and skip pseudo-filesystem mounts
fsmonitor scan / --metric unique --one-file-system --exclude-pseudo

# Create a saved monitor and capture its first snapshot
fsmonitor monitor add /path --interval 6h --capture-now
fsmonitor monitor list

# Add an audited growth alert to monitor 1
fsmonitor alerts add 1 /path --growth 10GiB --window 24h

# Watch a transient path, one saved monitor, or every enabled monitor
fsmonitor watch /path --interval 6h
fsmonitor watch --monitor 1
fsmonitor watch --all

# Explain growth between snapshots (target first, baseline second)
fsmonitor compare latest previous /path
fsmonitor compare --since 7d /path

# Create a read-only CleanupPlan (default; no filesystem changes)
fsmonitor cleanup /path

# Apply the saved plan using Trash/quarantine, inspect history, then undo
fsmonitor cleanup --plan PLAN_ID --apply
fsmonitor cleanup history --by category
fsmonitor cleanup undo PLAN_ID

# Inspect, validate, or toggle declarative rule packs
fsmonitor cleanup rules list
fsmonitor cleanup rules validate ./my-cleanup-rules.toml
fsmonitor cleanup rules disable node
fsmonitor cleanup rules enable node

# Revalidate and purge owned quarantine content (never system Trash)
fsmonitor cleanup purge PLAN_OR_ACTION_ID

# Permanent deletion is separate and requires the exact plan-scoped token
fsmonitor cleanup --plan PLAN_ID --permanent
```

Each scan reports a short run id, active phase and policy, and one explicit
terminal status (`completed`, `partial`, `cancelled`, or `failed`). The TUI,
one-shot CLI, and periodic watch use the same scan service and result semantics.

## Documentation

- **[User Guide](docs/user-guide.md)** -- Usage, configuration, CLI commands, all key bindings
- **[Architecture](docs/architecture.md)** -- Internals: scanner threading, database schema, visualization algorithms, screen management
- **[Filesystem Compatibility](docs/fs.md)** -- Supported filesystems, every syscall the tool makes, platform-specific behavior
- **[Release Process](docs/release-process.md)** -- Locked builds, clean-wheel smoke tests, checksums, SBOM, provenance, and PyPI publishing
- **[Delivery Waves](docs/waves/README.md)** -- Interactive Wave 01–07 feature, architecture, and validation briefs
- **[Contributing](docs/contributing.md)** -- Dev setup, testing, how to add rules/screens/visualizations
