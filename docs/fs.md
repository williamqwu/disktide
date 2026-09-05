# Filesystem Interactions & Compatibility

How disktide interacts with the filesystem, and what works (or breaks) on different filesystem types.

## Compatibility Summary

| Filesystem | Scanning | Watch | Cleanup | Notes |
|------------|----------|-------|---------|-------|
| ext4, XFS, Btrfs | Full | Full | Full | Primary target |
| ZFS | Full | Full | Full | |
| NTFS (via fuse) | Full | Full | Full | Case-preserving; see [Case Sensitivity](#case-sensitivity) |
| FAT32/exFAT (via fuse) | Full | Full | Full | No symlinks; mtime resolution is 2s |
| NFS/NFS4 | Full | Full | Full | 8 workers, more when the mount samples slow; see [Network Filesystems](#network-filesystems-nfs-cifs-sshfs) |
| CIFS/SMB | Full | Full | Full | Same tiering; `cifs`, `smb3` and `smbfs` all recognised |
| Ceph, GlusterFS, BeeGFS, Lustre, GPFS, PanFS, AFS, 9p | Full | Full | Full | Same tiering |
| sshfs, rclone, s3fs, gcsfuse, JuiceFS and other FUSE | Full | Full | Caution | Same tiering; deletion over sshfs can be slow |
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
| `/proc/meminfo` | Available memory | 0 MB (no low-memory override applied) |
| `/proc/mounts` | Filesystem type detection | `"unknown"`, conservative local fallback |
| `/sys/block/*/queue/rotational` | HDD vs SSD detection | `None`, metadata sample decides or falls back to serial |
| `os.sched_getaffinity(0)` | CPU count after a cpuset | Falls back to `os.cpu_count()` |
| `/proc/self/cgroup` + `cpu.max` / `cpu.cfs_quota_us` | CPU count after a *quota* | No quota; the cpuset alone decides |
| `memory.max` / `memory.limit_in_bytes` | Memory available after a container limit | Host-wide `/proc/meminfo` alone |
| `os.getloadavg()` | System load | `(0, 0, 0)` (no load-based reduction) |

The two control-group rows exist because neither interface above them can see a
container's limits. `sched_getaffinity` reports a cpuset, but `docker run
--cpus=1` sets a *quota* and leaves every core visible, so on a 64-core host
disktide believed it had 64 CPUs. `/proc/meminfo` is host-wide inside a
container, so under `--memory=512m` on a 256 GB host it believed ~200 GB were
free and the "under 512 MB, scan serially" guard never fired. Both hierarchies
are read, from the process's own group up to the root, and the tightest limit on
that chain is the one that binds -- a batch scheduler usually sets the limit on
the job, not on the task. A quota that binds makes the host `allocated`, so
host-wide load stops throttling the scan. `disktide doctor` prints both under
Platform.

On macOS, `/proc` and `/sys` do not exist. Scanning still works, while mount,
block-device, and medium detection report unavailable through `disktide doctor`
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
os.open(path, O_RDONLY|O_DIRECTORY|O_CLOEXEC)  # once per directory
                                 # + O_NOFOLLOW below the scan root
os.fstat(fd).st_mtime            # this directory's mtime + cycle-guard identity
os.scandir(fd)                   # iterate directory entries, fd-relative
  entry.is_symlink()             # classify: symlink?
  entry.is_dir(follow_symlinks=False)   # classify: directory?
  entry.is_file(follow_symlinks=False)  # classify: regular file?
  entry.stat(follow_symlinks=False)     # fstatat(fd, name): st_size, st_blocks,
                                        # dev/inode/nlink, mtime
  entry.name                     # basename (str)
os.close(fd)                     # two descriptors in flight per worker, no more

# Deferred work, runs on demand when the UI looks at a symlink
# (Details panel render, or `i` to navigate into a symlinked dir),
# plus eagerly for the first 100 symlinks at the scan root only:
  os.readlink(node.path)         # symlinks only: target string
  os.stat(node.path)             # symlinks only: classify target type
```

**Metadata captured per entry, directories included:** logical payload (`st_size`), allocated payload (`st_blocks * 512` when available -- a directory's own blocks come from the `fstat` of the descriptor it was opened on, so they cost no extra syscall), device/inode identity (`st_dev`, `st_ino`), hard link count (`st_nlink`), modification time (`st_mtime`), and type. For symlinks the target path and target type are populated lazily (see Symlink Handling below).

**Metadata NOT captured:** permissions, ownership (uid/gid), extended attributes, ACLs, creation time, filesystem compression ratio, reflink sharing, or snapshot-exclusive physical blocks.

`os.scandir()` is used instead of `os.listdir()` + `os.stat()` because it avoids a second syscall per entry on Linux (the kernel returns `d_type` from `getdents64`). It is handed a *descriptor* rather than a path so that each entry's stat is an `fstatat` of one name instead of an `lstat` of a whole path: measured on one thread with no node building, 3.14 us per entry against 3.58 warm on local xfs and 11.0 against 13.0 on warm NFSv4. The full child path is then joined in Python, which is what `FSNode.path` stores; it is never handed back to the kernel. `scanner/walker.py::scan_directory`, the recursive compatibility walker, is deliberately still path-based -- it recurses one Python frame per level, so it runs out of interpreter stack long before it runs out of pathname.

### Deep trees

PATH_MAX -- 4,096 bytes on Linux -- is a limit on the pathname *argument* of a syscall, not on the depth of a tree, and the scan no longer runs into it. Nothing below the scan root is ever named by its whole path, and the scan root's own path is opened in PATH_MAX-sized steps, each relative to the descriptor the last step returned (four opens for a 10,900-byte path). A chain of 1,200 directories with an eight-letter name at each level scans to the bottom; before this it stopped at level 442 and recorded `[Errno 36] File name too long` as a *denied* directory, so the tree looked like a permissions problem rather than a deep one.

Two things still use whole paths and still fail past 4,096 bytes: `os.readlink` and the follow-stat in `classify_symlink`, so a symlink deeper than PATH_MAX is reported as broken rather than classified; and every cleanup action -- trash, quarantine, delete -- which builds the full path to hand to `shutil` and the trash spec. Deleting a directory *containing* such a path works, because `shutil.rmtree` is fd-relative itself; naming one of its descendants directly does not.

### Symlink Handling

Symlinks are **never recursed into**. A symlink is stored as a leaf node sized by the link itself (`stat(follow_symlinks=False)`), never its target.

This prevents:
- Infinite loops from circular symlinks
- Double-counting when multiple symlinks point to the same target
- A symlinked directory's bytes inflating the parent total

Target classification (the `readlink` for the target string and the `stat(follow_symlinks=True)` to learn whether the target is a directory, a file, or broken) is **deferred to first use**: `make_symlink_node` pays only the link's own `entry.stat(follow_symlinks=False)`, and `classify_symlink(node)` runs the deferred work when the Details panel renders the node or the `i` action navigates a symlinked directory. Result is cached on the node (`link_classified=True`), so a second look is free.

To keep the typical `disktide ~` case showing the inline `→ target` decoration in the tree from the start, the engine eagerly classifies the first `_TOP_LEVEL_CLASSIFY_CAP = 100` symlinks it encounters at the scan root. Deeper symlinks remain fully lazy regardless of count. This costs at most ~60 ms of extra round-trips at scan start on slow shares; it cannot regress the case where the scan root itself is a directory containing hundreds of thousands of symlinks (a real shape: image-cache `.dataset/` trees on a cluster home), which used to add minutes to the scan.

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

**A coverage gap never turns a metric into "Unavailable".** A directory that could not be read still contributes its *own* blocks -- it was stat'ed from its parent even when it could not be opened -- and nothing from inside it. So does a directory stopped by `--max-depth`. A directory that vanished under the scan, one excluded as a pseudo mount or across a filesystem boundary, and one that is its own ancestor contribute zero; the last of those has already been counted under the ancestor that shares its inode. What is missing is reported by `inaccessible_count` / `inaccessible_subtree_count`, `depth_limited_subtree_count`, `excluded_subtree_count` and `vanished_subtree_count` -- the "Coverage: partial" line -- exactly as it always has been for Logical.

`None` ("Unavailable") in `allocated_size` / `unique_allocated_size` therefore means one thing and only one: this platform does not provide `st_blocks` at all, so no node in the tree has a number. The two shapes of unreadable therefore agree: a `chmod 000` directory (the open fails) and a `chmod 444` one (the listing succeeds, every `fstatat` under it fails) are both one inaccessible subtree and both report a number. `tests/scheduler_invariants.py` I8 fails any applied directory that reports `None` while all of its entries carry a number.

**Changed during the scan is not the same as unreadable.** An `OSError` whose
`errno` is `ENOENT`, `ESTALE` or `ENOTDIR` means the entry was listed by its
parent's `readdir` and was gone by the time the `stat` reached it -- the tree
moved under the walk, and nothing denied us anything. Those entries land in
`FSNode.vanished_count` / `vanished_subtree_count`, a directory that
disappeared before its own job ran is marked `vanished` with `error` left at
`None`, and none of it reaches `inaccessible_count`, the progress error count,
`AccessError` events or the run's `partial` status. Every other errno,
`PermissionError` included, behaves exactly as it did. The scan root is the
exception: a root that does not exist is a bad argument and still reports an
error.

### Monitor and snapshot repository -- `repositories/sqlite.py`, `storage/database.py`

SQLite database stored at `~/.local/share/disktide/data.db` for new installs.
When `~/.local/share/sizetrail/data.db` or the older
`~/.local/share/fsmonitor-cli/data.db` already exists and the new path does not,
DiskTide keeps using the newest available legacy database so monitor history
and cleanup audit records remain available.

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
`disktide[watch]` makes `inotify-simple` available through a lazy probe. A
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
`~/.config/disktide/cleanup-rules/*.toml`. Validation reads policy only:
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
os.open(parent, O_DIRECTORY | O_NOFOLLOW)
os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
os.rename(name, destination, src_dir_fd=parent_fd, dst_dir_fd=destination_fd)
Path.write_text(...)            # .trashinfo or quarantine recovery manifest
```

Quarantine directories must be owned by the current user, mode `0700`, and on
the same device as the target. The executor never substitutes copy+delete for an
atomic rename. A constant-size `.ledger.json` and lock provide normal-path
capacity accounting; manifests are scanned only for explicit audit/rebuild or
interrupted-state recovery. Undo uses a reverified dir-fd rename back to the
original path only when that path is absent and the isolated inode still matches
the plan identity.

Owned quarantine purge repeats manifest and identity checks, then removes only
the isolated quarantine object after the exact `PURGE <plan-id>` confirmation.
It does not enumerate or remove system Trash. The audit/history model records
isolated, purged, actual reclaimed, and undone bytes separately.

Permanent deletion is not the default executor. It is available only after a
plan-scoped typed confirmation. Supported POSIX systems stage a verified file or
symlink under the same parent and unlink it by directory fd. Direct permanent
directory deletion is blocked; directory content must first enter the owned
quarantine and can then be purged through a no-symlink dir-fd traversal. Product
CLI/TUI code cannot call this primitive without `CleanupService` revalidation
and a successful pre-action audit write. Rules marked detection-only cannot
reach either safe or permanent filesystem primitives.

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
It combines adapter probes with a bounded direct-directory metadata sample and
records the requested/effective count plus reason. The Linux I/O lives in
`collectors/platform/linux.py`; each probe returns an available, degraded, or
unavailable result with a reason.

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

### Directory Blocks

A directory is a file too: it holds names, and the filesystem charges it blocks for them. On ext4 that is 4 KiB for any directory at all; on xfs a small directory's names live in the inode and cost nothing, and one that outgrows it takes 4 KiB and then more (a 300-entry directory measures 12,288 bytes).

- **Logical** counts files and symlinks only. A directory's own `st_size` is *not* added: on ext4 it is 4,096 whatever the directory holds, and on xfs it is a byte count of the names themselves (24 for a directory holding two of them). Neither is payload anybody stored.
- **Allocated** is `st_blocks * 512` of every file, symlink **and directory** in the subtree, the node itself included. This is what `du` reports, and a scan of a tree with no files in it is no longer zero.
- **Unique** is the same after hardlink deduplication. A directory is never a hardlink duplicate -- no second path resolves to one directory inode -- so directory blocks pass through the dedup unchanged and appear once, under the directory that owns them.

`du` deduplicates hardlinks by inode, so on a tree with hardlinks `du -s --block-size=1` matches **Unique**, and Allocated is larger by the duplicated bytes.

A directory's *own* allocated bytes (`own_allocated_size`, the "Own allocated" row in the details panel) are its own blocks plus its direct files' and symlinks'. Sub-directories therefore no longer add up to their parent in the Allocated and Unique metrics -- the difference is the parent's own blocks -- exactly as they already did not in Logical, where the difference is the parent's direct files.

### Network Filesystems (NFS, CIFS, sshfs)

Worker count is chosen from a *measured* per-entry latency, not from the
filesystem name alone. The mount type (via `/proc/mounts`) decides whether the
scan is latency-bound at all; a 64-entry, 75 ms metadata sample of the scan root
then decides how many workers that buys:

| Sampled latency per entry | Workers |
|---|---|
| under 0.5 ms (warm NFS, GPFS) | 8 |
| 0.5 ms and up | 16 |
| 1 ms and up (sshfs, CIFS over a WAN) | 32 |
| 3 ms and up (an object store behind FUSE) | 64 |

The tiers come from a sweep that wrapped `os.scandir` so every entry cost a
fixed sleep and then ran the real engine: at 5 ms per entry the wall time falls
from 25.1 s at one worker to 3.38 s at eight and 0.88 s at 64, and at 1 ms it
bottoms out at 32. A fixed cap of 8 left roughly 3x on the table for those
mounts. The `min(available_cpus, 8)` cap that applies to local storage does not
apply here: a thread waiting on a server is descheduled for the whole wait and
does not need a core to hold it open.

Two limits still win over the tier. A *shared* host -- no CPU allocation and
other people's processes present, the cluster login-node case -- caps at 2
whatever the storage says, because the node building between the waits is real
CPU on a machine we are a guest on. Under 512 MB of available memory the scan
goes serial.

- Latency per `os.scandir()` call is higher, so scans take longer
- `st_mtime` may have lower resolution or be subject to clock skew between client and server
- The snapshot database is stored locally by default (`XDG_DATA_HOME`), not on the scanned network share

### FUSE Filesystems

FUSE filesystems (sshfs, rclone, s3fs) generally work since the scanner only uses standard POSIX calls. Performance depends entirely on the FUSE implementation. `os.scandir()` may not benefit from kernel `d_type` optimization through FUSE, falling back to per-entry `stat()` calls.

Every `fuse.*` mount is treated as latency-bound and tiered exactly as a network
mount is, whether or not its backend is remote and whether or not the specific
backend is named in `NETWORK_FS_TYPES`: a FUSE round trip is two context
switches to a userspace daemon at best and a request to an object store at
worst, and neither is something a thread can do without being descheduled. A
mount backed by an object store routinely samples above 3 ms per entry and takes
the 64-worker tier. `virtiofs` is *not* in that group -- it is a shared-memory
transport with local-order latency -- and neither is `overlay`.

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
