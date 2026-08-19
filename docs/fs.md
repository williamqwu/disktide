# Filesystem Interactions & Compatibility

How fsmonitor interacts with the filesystem, and what works (or breaks) on different filesystem types.

## Compatibility Summary

| Filesystem | Scanning | Watch | Cleanup | Notes |
|------------|----------|-------|---------|-------|
| ext4, XFS, Btrfs | Full | Full | Full | Primary target |
| ZFS | Full | Full | Full | |
| NTFS (via fuse) | Full | Full | Full | Case-preserving; see [Case Sensitivity](#case-sensitivity) |
| FAT32/exFAT (via fuse) | Full | Full | Full | No symlinks; mtime resolution is 2s |
| NFS/NFS4 | Full | Full | Full | Workers capped at 4; latency may be high |
| CIFS/SMB | Full | Full | Full | Workers capped at 4 |
| sshfs (FUSE) | Full | Full | Caution | Workers capped at 4; deletion over sshfs can be slow |
| tmpfs, ramfs | Full | Full | Full | |
| OverlayFS | Partial | Partial | Caution | Sees merged view; deletions affect upper layer only |
| macOS APFS/HFS+ | Scanning works | Watch works | Cleanup works | sysinfo falls back to defaults; see [Platform](#platform-support) |
| Windows NTFS | Not supported | Not supported | Not supported | See [Platform](#platform-support) |

## Platform Support

The core scanner (`os.scandir`, `os.stat`, `os.path`) is cross-platform Python. However, the adaptive threading system in `scanner/sysinfo.py` reads Linux-specific interfaces:

| Interface | Purpose | Fallback when absent |
|-----------|---------|---------------------|
| `/proc/meminfo` | Available memory | 0 MB (no low-memory cap applied) |
| `/proc/mounts` | Filesystem type detection | `"unknown"`, not flagged as network FS |
| `/sys/block/*/queue/rotational` | HDD vs SSD detection | `None` (no I/O cap applied) |
| `os.sched_getaffinity(0)` | cgroup-aware CPU count | Falls back to `os.cpu_count()` |
| `os.getloadavg()` | System load | `(0, 0, 0)` (no load-based reduction) |

On macOS, `/proc` and `/sys` don't exist. All sysinfo functions catch `OSError`/`AttributeError` and return safe defaults, so scanning works -- but worker count won't adapt to storage type or filesystem. Set `workers` in config explicitly on non-Linux systems.

FS Overview is currently Linux-oriented: mount discovery reads `/proc/mounts`, and the block-device panel uses `lsblk`. On platforms without those interfaces, scanning still works but FS Overview may be empty or omit the block-device panel.

On Windows, `os.scandir` and `os.stat` work, but `os.getloadavg()` and `os.sched_getaffinity()` don't exist (`AttributeError` caught). The deeper issue is that the project hasn't been tested on Windows and the TUI depends on terminal capabilities that may not work under cmd.exe (Textual has partial Windows support via Windows Terminal).

## Every Filesystem Touchpoint

### Scanning -- `scanner/walker.py`, `scanner/engine.py`

The scanner is the most filesystem-intensive component. Here is exactly what it calls for each directory:

```
os.stat(path).st_mtime           # root directory mtime + cycle-guard identity
os.scandir(path)                 # iterate directory entries
  entry.is_symlink()             # classify: symlink?
  entry.is_dir(follow_symlinks=False)   # classify: directory?
  entry.is_file(follow_symlinks=False)  # classify: regular file?
  entry.stat(follow_symlinks=False)     # read st_size, st_mtime (incl. symlinks)
  entry.name                     # basename (str)
  entry.path                     # full path (str)

# Deferred work, runs on demand when the UI looks at a symlink
# (Details panel render, or `i` to navigate into a symlinked dir),
# plus eagerly for the first 100 symlinks at the scan root only:
  os.readlink(node.path)         # symlinks only: target string
  os.stat(node.path)             # symlinks only: classify target type
```

**Metadata captured per entry:** size (`st_size`), modification time (`st_mtime`), type (dir/file/symlink). For symlinks the target path and target type are populated lazily (see Symlink Handling below).

**Metadata NOT captured:** permissions, ownership (uid/gid), inode number, extended attributes, ACLs, creation time, hard link count.

`os.scandir()` is used instead of `os.listdir()` + `os.stat()` because it avoids a second syscall per entry on Linux (the kernel returns `d_type` from `getdents64`).

### Symlink Handling

Symlinks are **never recursed into**. A symlink is stored as a leaf node sized by the link itself (`stat(follow_symlinks=False)`), never its target.

This prevents:
- Infinite loops from circular symlinks
- Double-counting when multiple symlinks point to the same target
- A symlinked directory's bytes inflating the parent total

Target classification (the `readlink` for the target string and the `stat(follow_symlinks=True)` to learn whether the target is a directory, a file, or broken) is **deferred to first use**: `make_symlink_node` pays only the link's own `entry.stat(follow_symlinks=False)`, and `classify_symlink(node)` runs the deferred work when the Details panel renders the node or the `i` action navigates a symlinked directory. Result is cached on the node (`link_classified=True`), so a second look is free.

To keep the typical `fsmonitor ~` case showing the inline `→ target` decoration in the tree from the start, the engine eagerly classifies the first `_TOP_LEVEL_CLASSIFY_CAP = 100` symlinks it encounters at the scan root. Deeper symlinks remain fully lazy regardless of count. This costs at most ~60 ms of extra round-trips at scan start on slow shares; it cannot regress the case where the scan root itself is a directory containing hundreds of thousands of symlinks (a real shape: image-cache `.dataset/` trees on a cluster home), which used to add minutes to the scan.

The scan still never traverses the link.

### Error Handling

Every filesystem call is wrapped in try/except at the individual entry level:

```python
for entry in os.scandir(path):
    try:
        # process entry
    except OSError:
        continue  # skip this entry, scan the rest
```

A `PermissionError` on the directory itself (`os.scandir()` fails) records the error in the node and returns. A `PermissionError` on a single entry skips that entry. The scan always produces a result, even if partial.

Errors are stored in `FSNode.error` and displayed in the TUI details panel.

### Database -- `storage/database.py`

SQLite database stored at `~/.local/share/fsmonitor-cli/data.db` (XDG-compliant; the legacy directory name is retained for upgrade compatibility).

| Operation | System call |
|-----------|------------|
| Locate DB | `os.environ.get("XDG_DATA_HOME")`, `os.path.expanduser("~/.local/share")` |
| Create dir | `os.makedirs(db_dir, exist_ok=True)` |
| Open/create DB | `sqlite3.connect(path)` |

Snapshot roots are stored as absolute strings in `snapshots.root_path`; directory paths are interned in `paths.path` and referenced by integer IDs from baseline and delta rows. Query filtering uses exact or ancestor/descendant string comparison. No additional normalization is applied at the database layer -- CLI roots are stored after `Path.resolve()`.

SQLite pragmas: `journal_mode=WAL` (allows concurrent readers with one writer), `foreign_keys=ON`.

On network filesystems, placing the database on the network share would be slow. The default XDG path puts it on the local filesystem, which is correct.

### Configuration -- `config.py`

| Operation | System call |
|-----------|------------|
| Locate config | `os.environ.get("XDG_CONFIG_HOME")`, `os.path.expanduser("~/.config")` |
| Create dir | `Path.parent.mkdir(parents=True, exist_ok=True)` |
| Check exists | `Path.exists()` |
| Read | `open(config_file, "rb")` + `tomllib.load()` |
| Write | `Path.write_text()` |

### Cleanup -- `cleanup/actions.py`, `cleanup/detector.py`, `models/patterns.py`

Detection primarily operates on the in-memory `FSNode` tree. Parent-indicator rules perform live existence checks, and deletion performs direct filesystem operations:

**Parent indicator checks** (`patterns.py`):
```python
(Path(parent_path) / indicator_name).exists()
```
This checks whether files like `package.json` or `Cargo.toml` exist next to a candidate target.

**Deletion** (`actions.py`):
```python
os.path.isdir(target.path)      # directory or file?
os.path.islink(target.path)     # unlink a symlink itself, never its target
shutil.rmtree(target.path)      # recursive directory delete
os.path.exists(target.path)     # existence check
os.unlink(target.path)          # single file delete
```

Deletion is permanent and does not use trash/quarantine, undo, persistent audit logging, or stale-target revalidation. The action checks whether the current path is a symlink before directory detection, so a directory symlink is unlinked without touching its target. A dry-run path exercises result reporting without making these calls.

### Welcome Screen -- `screens/welcome.py`

Path completion uses `os.scandir()` and `os.path` functions:

```python
os.path.expanduser(value)       # ~ expansion
os.path.dirname(expanded)       # parent directory
os.path.basename(expanded)      # partial name
os.path.isdir(parent_dir)       # validate directory
os.scandir(parent_dir)          # list entries for completion
entry.is_dir(follow_symlinks=False)  # trailing / for dirs
os.path.join(parent, name)      # construct full path
Path(raw).resolve()             # resolve before submitting
```

### System Info -- `scanner/sysinfo.py`

Linux-specific reads (all wrapped in try/except with safe fallbacks):

```python
os.cpu_count()                  # CPU count
os.sched_getaffinity(0)         # cgroup-aware CPU count
os.getloadavg()                 # 1/5/15-min load averages
open("/proc/meminfo")           # total and available memory
open("/proc/mounts")            # filesystem type, mountpoint
os.path.realpath(path)          # resolve symlinks for device lookup
os.path.exists("/sys/block/...") # check sysfs paths
open("/sys/block/.../rotational") # HDD vs SSD
os.listdir("/sys/block/.../slaves") # device-mapper slave devices
os.path.basename(os.path.realpath(dev)) # resolve /dev symlinks
```

### FS Overview -- `screens/fs_overview.py`, `scanner/blockdev.py`, `scanner/benchmark.py`

Mounted-filesystem discovery and capacity reporting use:

```python
open("/proc/mounts")            # device, mountpoint, fs type, options
os.statvfs(mountpoint)          # blocks, available space, inode counts
subprocess.run(["quota", ...]) # optional current-user quota data
subprocess.run(["lsblk", ...]) # optional JSON block-device tree
```

`statvfs` for network mounts runs in a worker with a 3-second timeout so a stale NFS/CIFS mount cannot block the screen indefinitely. Local mounts are queried directly. Pseudo-filesystems and zero-capacity mounts are filtered from the table.

The `b` action is explicitly opt-in and confirmed before writing. It uses `tempfile.mkstemp()` on the selected mount (mode 0600), writes at most 256 MiB and at most 25% of currently available space, calls `os.fsync()`, makes a best-effort `posix_fadvise(..., DONTNEED)` cache drop, reads the file back, and always unlinks it. The read rate is approximate because the cache-drop request is advisory.

## Filesystem-Specific Considerations

### Case Sensitivity

The scanner stores paths exactly as returned by `os.scandir()`. On case-insensitive filesystems (NTFS via FUSE, macOS HFS+/APFS), the same directory could theoretically be reached via different casings, but since the scanner always walks from the root using `scandir()` results, it uses the filesystem's canonical casing. Database queries use exact string matching, so this is consistent as long as the same root path casing is used across scans.

### Sparse Files

`st_size` reports the logical size, not the on-disk allocation. A 1 GB sparse file with only 4 KB allocated will show as 1 GB. This matches what `du --apparent-size` reports, but differs from `du` (which reports allocated blocks). There is no `st_blocks` tracking.

### Hard Links

Each hard link is counted independently. If the same inode is linked from two paths, its size is counted twice. There is no inode-based deduplication. For most use cases this doesn't matter, but on filesystems with heavy hard link usage (e.g., some backup systems, Nix store), reported sizes may exceed actual disk usage.

### Network Filesystems (NFS, CIFS, sshfs)

- Worker count is automatically capped at 4 when a network filesystem is detected (via `/proc/mounts`)
- Latency per `os.scandir()` call is higher, so scans take longer
- `st_mtime` may have lower resolution or be subject to clock skew between client and server
- The snapshot database is stored locally by default (`XDG_DATA_HOME`), not on the scanned network share

### FUSE Filesystems

FUSE filesystems (sshfs, rclone, s3fs) generally work since the scanner only uses standard POSIX calls. Performance depends entirely on the FUSE implementation. `os.scandir()` may not benefit from kernel `d_type` optimization through FUSE, falling back to per-entry `stat()` calls.

### OverlayFS

The scanner sees the merged view. Size reporting is correct for what's visible. Cleanup deletions only affect the upper (writable) layer. A file deleted from the overlay may still exist in the lower layer and reappear if the overlay is reconstructed.

### tmpfs / ramfs

Works correctly. Note that scanning a tmpfs reports RAM-backed file sizes. This is disk usage tooling, so the numbers may be misleading for tmpfs-backed directories like `/tmp` or `/dev/shm`.

### FAT32 / exFAT

No symlink support (symlinks don't exist on FAT). Modification time resolution is 2 seconds on FAT32. File sizes are accurate.

### Btrfs Considerations

`st_size` reports logical size, not deduplicated or compressed size. Btrfs snapshots, subvolumes, and reflinks mean the actual disk savings from deduplication won't be visible. The tool reports apparent sizes, not physical allocation.

### ZFS

Similar to Btrfs -- deduplication and compression mean apparent sizes may differ from physical usage. ZFS snapshots are not visible through the normal directory tree, so they don't affect scanning.

### procfs / sysfs / devfs

These virtual filesystems can be scanned but the results are meaningless for disk usage purposes. The scanner reports whatever `st_size` the kernel returns (often 0 for procfs entries), so choose a narrower scan root instead of scanning a tree that crosses into them. FS Overview filters pseudo-filesystems automatically.
