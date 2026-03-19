# fsmonitor-cli

Interactive terminal disk usage explorer built with Python and Textual.

## Installation

```bash
pip install -e .
```

## Usage

```bash
# Launch interactive TUI
fsmonitor-cli /path/to/explore

# CLI scan
fsmonitor-cli scan /path --snapshot

# Watch mode
fsmonitor-cli watch /path --interval 6h

# Cleanup mode
fsmonitor-cli cleanup /path
```

## Key Bindings

| Key | Action |
|-----|--------|
| Up/Down | Navigate tree |
| Enter | Drill into directory |
| Backspace | Go up one level |
| Tab | Switch focus |
| 1/2/3 | Switch visualization |
| Ctrl+E/D/M | Switch mode (Explorer/Cleanup/Monitor) |
| s | Cycle sort |
| r | Rescan |
| q | Quit |
