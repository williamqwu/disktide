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

**Stored data.** The app follows XDG conventions and writes to two persistent locations. Deleting either is safe for the filesystem, but resets the corresponding settings or snapshot history. The existing `fsmonitor-cli` directory names are intentionally retained so upgrades keep current data:

| Location | Contents | Typical size |
|----------|----------|--------------|
| `~/.config/fsmonitor-cli/config.toml` | User settings | < 1 KB |
| `~/.local/share/fsmonitor-cli/data.db` | SQLite snapshot store (policy metadata plus file/directory baselines and deltas) | Depends on tree size and snapshot count |

Snapshot data is written by the CLI commands `scan --snapshot` and `watch`.
`fsmonitor compare` checks policy and root compatibility before reporting
growth. The TUI opens the repository for recent paths and Monitor mode but does
not save snapshots. Old snapshots are pruned automatically based on retention
settings (default: 30 days).

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
| `e` / `m` / `f` | Switch mode (Explorer / Monitor / FS Overview) |
| `c` | Switch to experimental Cleanup mode (enable it in Settings first) |
| `1` / `2` / `3` | Switch visualization (Sunburst / Treemap / Details) |
| `u` / `i` | Navigate up / drill into directory |
| `s` | Cycle sort (Size / Name / Modified) |
| `y` / `t` | Copy highlighted path / cycle Logical, Allocated, Unique, Files |
| `r` | Rescan / refresh |
| `b` | Benchmark the highlighted mount in FS Overview (after confirmation) |
| `?` | Settings |
| `q` | Quit |

### CLI Commands

```bash
# Explain this installation and its available platform features
fsmonitor doctor
fsmonitor doctor --json

# Scan a directory directly
fsmonitor scan /path --metric allocated --snapshot

# Stay on one filesystem and skip pseudo-filesystem mounts
fsmonitor scan / --metric unique --one-file-system --exclude-pseudo

# Watch for changes over time
fsmonitor watch /path --interval 6h

# Explain growth between snapshots (target first, baseline second)
fsmonitor compare latest previous /path
fsmonitor compare --since 7d /path

# Find candidates and permanently delete all of them after confirmation
fsmonitor cleanup /path
```

Each scan reports a short run id, active phase and policy, and one explicit
terminal status (`completed`, `partial`, `cancelled`, or `failed`). The TUI,
one-shot CLI, and periodic watch use the same scan service and result semantics.

## Documentation

- **[User Guide](docs/user-guide.md)** -- Usage, configuration, CLI commands, all key bindings
- **[Architecture](docs/architecture.md)** -- Internals: scanner threading, database schema, visualization algorithms, screen management
- **[Filesystem Compatibility](docs/fs.md)** -- Supported filesystems, every syscall the tool makes, platform-specific behavior
- **[Release Process](docs/release-process.md)** -- Locked builds, clean-wheel smoke tests, checksums, SBOM, provenance, and PyPI publishing
- **[Delivery Waves](docs/waves/README.md)** -- Interactive Wave 01–05 feature, architecture, and validation briefs
- **[Contributing](docs/contributing.md)** -- Dev setup, testing, how to add rules/screens/visualizations
