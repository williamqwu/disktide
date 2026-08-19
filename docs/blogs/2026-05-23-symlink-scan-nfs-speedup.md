# Three syscalls to one: hunting a 10x symlink-scan regression over NFS

*v0.1.5 release postmortem.*

## The symptom

Between v0.1.3 and v0.1.5, scanning one cluster home directory regressed from ~4 minutes to ~40 minutes. Same machine, same content, same NFS mount: 6.1M files, 100 GB. The TUI eventually finished, but progress sat visibly frozen for tens of seconds at a stretch while one worker ground through a deep subtree.

## The hunt

Streaming per-second progress (`tool/diag_scan.py`) pinpointed the slow leaves: a `dataset/` tree where each subdirectory contained on the order of 200,000 symbolic links into a shared image cache. Re-running on one such directory with `--workers 1 --profile` made the culprit obvious. Wall time was 89.5 s for 215,339 entries; the cProfile top callees were:

```
215,339  posix.stat                       81.0 s  classify_symlink (target follow)
215,339  posix.DirEntry.stat               0.7 s  link's own stat
215,339  posix.readlink                    0.3 s  link target text
```

The walker was paying **three NFS round-trips per symlink** where v0.1.3 paid one:

1. `entry.stat(follow_symlinks=False)` for the link's size. Unavoidable, this is what makes the FSNode.
2. `os.readlink(path)` for the target text. Unconditional, added in v0.1.4 to display the inline `-> /target` decoration in the tree.
3. `os.stat(path)` to follow the link and classify the target as directory / file / broken. Also v0.1.4, made lazy for "deep" symlinks in an earlier v0.1.5 patch, but still eager whenever the symlink sat at the scan root.

On local SSD each stat is microseconds and nobody notices. On a clustered NFS share where each RPC is ~300 µs, three calls per entry across millions of symlinks is the entire regression.

## The fix

Make all symlink target info lazy. The walker now pays one syscall per symlink during the scan; `readlink` and the follow-stat run only when the UI actually looks at a link, and the result is cached on the node so a second look is free:

```python
def make_symlink_node(entry, depth):
    st = entry.stat(follow_symlinks=False)         # one syscall, always
    return FSNode(..., is_symlink=True)

def classify_symlink(node):                        # called on demand by the UI
    if node.link_classified: return
    node.link_target = os.readlink(node.path)      # readlink, deferred
    node.link_is_dir = stat.S_ISDIR(os.stat(node.path).st_mode)
    node.link_classified = True
```

The tree label degrades cleanly: a symlink the user has not selected yet shows just its name; the inline `-> /target` arrow appears once they look at it. The Details panel and the `i` action already called `classify_symlink` defensively, so the UI side needed no change.

One small refinement keeps the typical-case UX intact. A user running `fsmonitor ~` reasonably expects to see what the handful of symlinks at home root point to without having to click each one. So the engine eagerly classifies the **first 100 symlinks at the scan root** (and only at the scan root: deeper levels stay fully lazy). 100 is small enough to be invisible (60 ms of NFS RTT in the worst case), large enough to cover any non-pathological home, and bounded so it cannot regress the case where the scan root itself contains 215,000 symlinks.

## What it costs

Three small, honest trade-offs versus the old eager scheme:

1. **Inline `-> /target` arrow in the tree.** Visible for the first 100 symlinks at the scan root and for any symlink the user has already selected once (the result is cached on the node). Symlinks the user has not visited show just the name. The Details panel is still authoritative for any link the user looks at.
2. **First-select latency.** Selecting an unvisited symlink runs one `readlink` + one `os.stat` before the Details panel renders. ~600 µs on the cluster NFS, microseconds on local SSD. Subsequent selects of the same link are free.
3. **Sticky tree labels.** The tree widget computes a row's label when the row is first added; `classify_symlink` mutates the node but does not trigger a label re-render, so a row that was rendered before its link was visited keeps its name-only label until the next refresh (metric toggle, sort change, or re-expansion). Stale label, never stale data.

The scan total, the file/dir counts, the per-link size, the cycle and access markers, and the `i` action all behave exactly as before.

## The numbers

Isolated leaf, 215,339 symlinks, `--workers 1`, back-to-back in one session:

| build                | wall time | rate (files/sec) | `posix.stat` calls |
| -------------------- | --------- | ---------------- | ------------------ |
| v0.1.5 pre-fix       | 89.5 s    |  2,406           | 215,341            |
| v0.1.5 post-fix      |  8.4 s    | 25,706           | ~2                 |

**10.7x faster on the same content, same machine, same NFS share.** The dominant per-symlink syscall is gone; what's left is `entry.stat` for the link's own size, exactly as in v0.1.3. (The cap-100 refinement was added after this measurement; it adds at most 200 deterministic syscalls at the scan root, ~60 ms on this NFS, below the noise floor of the bench.)

End-to-end on the full home directory (6.1M files, 100 GB, default worker pool):

| build              | wall time | rate (files/sec) |
| ------------------ | --------- | ---------------- |
| v0.1.3 baseline    | 244 s     | 25,010           |
| v0.1.5 pre-fix     | 2,386 s   |  2,558           |
| v0.1.5 post-fix    | 195 s     | 31,268           |

**12.2x recovery from the regression, and 20% under the v0.1.3 baseline.** The headline regression of "the scan never finishes" becomes "the scan finishes a little faster than the version it regressed from".

## The takeaway

When adding a "rich UI" feature, count the syscalls per item on the hot path. A feature that adds two syscalls per file is invisible on local SSD and a 10x regression on remote storage. The fix is almost always the same shape: defer until the user actually asks to see it, and cache the answer.

Two related things made this easy to catch and easy to fix:

- A one-shot diagnostic script (`tool/diag_scan.py`) with a 1-second heartbeat, a stall detector, and an optional `cProfile` pass. Without the profile, the regression looked like "NFS is slow today". With the profile, it took one screen of output to name the function.
- A version-agnostic bench (`tool/bench_scan.py`) usable against any prior release to establish a fair baseline. The 244 s v0.1.3 number was the anchor that turned "this feels slow" into "this is 10x slow".
