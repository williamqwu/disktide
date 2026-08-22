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

The core scanner (`os.scandir`, `os.stat`, `os.path`) is cross-platform Python.
Platform-specific probes are isolated under `collectors/platform/`. Linux uses
procfs, sysfs, and optional system commands; macOS, Windows, and unknown
platforms use conservative adapters that return structured unavailable reasons
instead of raising into the scanner or UI.

| Interface | Purpose | Fallback when absent |
|-----------|---------|---------------------|
| `/proc/meminfo` | Available memory | 0 MB (no low-memory cap applied) |
| `/proc/mounts` | Filesystem type detection | `"unknown"`, not flagged as network FS |
| `/sys/block/*/queue/rotational` | HDD vs SSD detection | `None` (no I/O cap applied) |
| `os.sched_getaffinity(0)` | cgroup-aware CPU count | Falls back to `os.cpu_count()` |
| `os.getloadavg()` | System load | `(0, 0, 0)` (no load-based reduction) |

On macOS, `/proc` and `/sys` do not exist. Scanning still works, while mount,
block-device, and medium detection report unavailable through `fsmonitor doctor`
and FS Overview. Set `workers` explicitly on platforms where storage-medium
autodetection is unavailable.

FS Overview consumes the same adapter results. On platforms without mount or
`lsblk` support, it displays the capability status, reason, and remediation
instead of silently presenting an empty panel.

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
  entry.stat(follow_symlinks=False)     # st_size, st_blocks, dev/inode/nlink, mtime
  entry.name                     # basename (str)
  entry.path                     # full path (str)

# Deferred work, runs on demand when the UI looks at a symlink
# (Details panel render, or `i` to navigate into a symlinked dir),
# plus eagerly for the first 100 symlinks at the scan root only:
  os.readlink(node.path)         # symlinks only: target string
  os.stat(node.path)             # symlinks only: classify target type
```

**Metadata captured per entry:** logical payload (`st_size`), allocated payload (`st_blocks * 512` when available), device/inode identity (`st_dev`, `st_ino`), hard link count (`st_nlink`), modification time (`st_mtime`), and type. For symlinks the target path and target type are populated lazily (see Symlink Handling below).

**Metadata NOT captured:** permissions, ownership (uid/gid), extended attributes, ACLs, creation time, filesystem compression ratio, reflink sharing, or snapshot-exclusive physical blocks.

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

### Monitor and snapshot repository -- `repositories/sqlite.py`, `storage/database.py`

SQLite database stored at `~/.local/share/fsmonitor-cli/data.db` (XDG-compliant; the legacy directory name is retained for upgrade compatibility).

| Operation | System call |
|-----------|------------|
| Locate DB | `os.environ.get("XDG_DATA_HOME")`, `os.path.expanduser("~/.local/share")` |
| Create dir | `os.makedirs(db_dir, exist_ok=True)` |
| Open/create DB | `sqlite3.connect(path)` |
| Open read-only recovery | SQLite URI with `mode=ro`, then `PRAGMA query_only=ON` |
| Pre-migration backup | SQLite backup API to `data.db.pre-v7.bak` |
| Integrity probe | `PRAGMA quick_check` |
| Budget measurement | `Path.stat()` on the database, WAL, and shared-memory files |
| Retention compaction | `PRAGMA wal_checkpoint(TRUNCATE)` followed by `VACUUM` |

Monitor definitions and snapshot roots are stored as absolute strings;
definition paths are normalized with `Path.expanduser().resolve()`. File and
directory paths are interned in `paths.path` and referenced by integer IDs from
baseline and delta rows. Query filtering uses exact or ancestor/descendant
string comparison. The repository also stores monitor leases/status, pins,
rollup provenance, retention audits, and alert rule/event history; none of
those tables causes an additional filesystem walk.

SQLite pragmas: `journal_mode=WAL` (allows concurrent readers with one writer), `foreign_keys=ON`.

Schema migration is transactional. If write/migration setup fails but the file
is readable, history opens read-only; if it is corrupt, scanning continues with
an in-memory degraded repository. The original database is never automatically
deleted or overwritten.

On network filesystems, placing the database on the network share would be slow. The default XDG path puts it on the local filesystem, which is correct.

### Optional filesystem events -- `collectors/events/`, `services/watch.py`

The core install performs no native watch calls. On Linux, installing
`fsmonitor-cli[watch]` makes `inotify-simple` available through a lazy probe. A
held monitor lease recursively adds directory watches while respecting
`one_file_system`, pseudo-filesystem exclusion, maximum depth, and never-follow
symlink policy. Newly created directories receive watches before later events
are consumed when possible.

The backend emits normalized create, modify, delete, move, overflow, root-lost,
and backend-error hints. Rename cookies are paired inside the adapter; unmatched
moves become deletes after a bounded timeout. `Q_OVERFLOW`, watch-limit errors,
unmounts, root replacement, backend restart, and expired host leases mark the
monitor degraded and require a full reconciliation.

Ordinary hints are projected to their containing parent/subtree and coalesced by
`DirtyPathTracker`. Local reconciliation calls the same `ScanService`, metric,
and policy contract but does not write a snapshot. Periodic/manual/recovery full
scans remain authoritative. Consequently the event stream is not a filesystem
audit log, and process downtime is never represented as complete event history.

### Capacity alerts -- `services/alerts.py`

Free-space and free-inode alert rules call `os.statvfs(rule.path)` after a
monitor snapshot is saved. They read filesystem capacity metadata only; they do
not traverse the rule path. Size, growth, and new-large-item rules evaluate the
persisted snapshot measurements and make no extra filesystem calls.

### Configuration -- `config.py`

| Operation | System call |
|-----------|------------|
| Locate config | `os.environ.get("XDG_CONFIG_HOME")`, `os.path.expanduser("~/.config")` |
| Create dir | `Path.parent.mkdir(parents=True, exist_ok=True)` |
| Check exists | `Path.exists()` |
| Read | `open(config_file, "rb")` + `tomllib.load()` |
| Write | `Path.write_text()` |

### Cleanup -- `services/cleanup.py`, `cleanup/actions.py`, `cleanup/detector.py`, `extensions/cleanup_rules.py`

Detection primarily operates on the in-memory `FSNode` tree. Parent-indicator
rules perform live existence checks. Built-in declarative rule packs are read
through `importlib.resources`; user packs are parsed with `tomllib` from
`~/.config/fsmonitor-cli/cleanup-rules/*.toml`. Validation reads policy only:
the schema has no shell, Python, or executor hook. Creating a plan additionally
captures live identity without modifying the target:

**Parent indicator checks** (`patterns.py`):
```python
(Path(parent_path) / indicator_name).exists()
```
This checks whether files like `package.json` or `Cargo.toml` exist next to a candidate target.

**Revalidation and isolation** (`services/cleanup.py`, `actions.py`):
```python
os.lstat(target.path)           # device/inode/type/mtime/size; never follows links
os.scandir(target.path)         # re-measure directory contents before action
os.path.ismount(target.path)    # root/mount protection
os.rename(source, trash_path)   # same-filesystem system Trash move
os.rename(source, quarantine)   # atomic quarantine fallback
Path.write_text(...)            # .trashinfo or quarantine recovery manifest
```

Quarantine directories must be owned by the current user, mode `0700`, and on
the same device as the target. The executor never substitutes copy+delete for an
atomic rename. Undo uses `os.rename()` back to the original path only when that
path is absent and the isolated inode still matches the plan identity.

Owned quarantine purge repeats manifest and identity checks, then removes only
the isolated quarantine object after the exact `PURGE <plan-id>` confirmation.
It does not enumerate or remove system Trash. The audit/history model records
isolated, purged, actual reclaimed, and undone bytes separately.

Permanent deletion is not the default executor. It is available only after a
plan-scoped typed confirmation and then uses `os.unlink()` for files/symlinks or
`shutil.rmtree()` for real directories. Product CLI/TUI code cannot call this
primitive without `CleanupService` revalidation and a successful pre-action
audit write. Rules marked detection-only cannot reach either safe or permanent
filesystem primitives.

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

### Platform Adapters and System Info

`scanner/sysinfo.py` remains the compatibility facade used by worker tuning.
The Linux I/O lives in `collectors/platform/linux.py`; each probe returns an
available, degraded, or unavailable result with a reason.

```python
os.cpu_count()                  # CPU count
os.sched_getaffinity(0)         # cgroup-aware CPU count
os.getloadavg()                 # 1/5/15-min load averages
open("/proc/meminfo")           # total and available memory
open("/proc/self/mounts")       # filesystem type, mountpoint
os.path.realpath(path)          # resolve symlinks for device lookup
os.path.exists("/sys/block/...") # check sysfs paths
open("/sys/block/.../rotational") # HDD vs SSD
os.listdir("/sys/block/.../slaves") # device-mapper slave devices
os.path.basename(os.path.realpath(dev)) # resolve /dev symlinks
```

### FS Overview -- `screens/fs_overview.py`, platform adapter, benchmark

Mounted-filesystem discovery and capacity reporting use:

```python
adapter.enumerate_mounts()      # structured mount capability + records
os.statvfs(mountpoint)          # blocks, available space, inode counts
subprocess.run(["quota", ...]) # optional current-user quota data
adapter.list_block_devices()    # structured lsblk capability + device tree
```

`statvfs` for network mounts runs in a worker with a 3-second timeout so a stale NFS/CIFS mount cannot block the screen indefinitely. Local mounts are queried directly. Pseudo-filesystems and zero-capacity mounts are filtered from the table.

The `b` action is explicitly opt-in and confirmed before writing. It uses `tempfile.mkstemp()` on the selected mount (mode 0600), writes at most 256 MiB and at most 25% of currently available space, calls `os.fsync()`, makes a best-effort `posix_fadvise(..., DONTNEED)` cache drop, reads the file back, and always unlinks it. The read rate is approximate because the cache-drop request is advisory.

## Filesystem-Specific Considerations

### Case Sensitivity

The scanner stores paths exactly as returned by `os.scandir()`. On case-insensitive filesystems (NTFS via FUSE, macOS HFS+/APFS), the same directory could theoretically be reached via different casings, but since the scanner always walks from the root using `scandir()` results, it uses the filesystem's canonical casing. Database queries use exact string matching, so this is consistent as long as the same root path casing is used across scans.

### Sparse Files

Logical uses `st_size`; Allocated uses `st_blocks * 512`. A 1 GB sparse file with only 4 KB allocated therefore shows roughly 1 GB in Logical and 4 KB in Allocated/Unique. On platforms without `st_blocks`, Allocated and Unique are shown as `Unavailable`, not zero.

### Hard Links

Logical and Allocated count each visible path independently. Unique groups entries by `(st_dev, st_ino)` and assigns the allocated bytes to the lexicographically first absolute path in the scan root; other links show zero Unique bytes and identify the owner. This is deterministic across worker counts. Equal inode numbers on different devices are not deduplicated.

Directory metadata blocks are not included in the 0.2.0 metrics, so Allocated/Unique can differ slightly from `du`, which also accounts for directory blocks.

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

Descendant pseudo-filesystem mountpoints are excluded by default and remain visible as policy-excluded boundary nodes. The scan root itself is never excluded, so explicitly scanning `/proc`, tmpfs, or an overlay root still works. Use `--include-pseudo` or `scan.exclude_pseudo_filesystems = false` to include descendant pseudo filesystems. FS Overview and the scanner share the same pseudo-filesystem classification.
