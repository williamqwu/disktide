"""Pure-Python `scan_dir`, tuple for tuple with the C extension.

This is what runs when `disktide.scanner._scanfast` was not built for the
running interpreter -- a platform with no wheel, an sdist install with no
compiler, or `DISKTIDE_ACCEL=0`. It is also the reference the tests compare
the extension against, which is why it goes out of its way to produce the
*same tuple*, not merely an equivalent one: the scheduler's entry loop is
shared by both backends, so anything the two disagree about would show up as
a difference in the scanned tree.

The one syscall per entry the C version saves is the whole point, so nothing
here tries to be clever about it: `os.scandir(fd)` answers `d_type` for free
and `DirEntry.stat(follow_symlinks=False)` is the same `fstatat` the
extension makes, one GIL round trip at a time.

Two documented near-misses, both requiring a stat that fails on an entry
that is neither a file, a directory nor a symlink:

* the `d_type` of a fifo, socket or device node whose stat failed reads back
  as `DT_UNKNOWN` here (a `DirEntry` cannot name those types without the
  stat that just failed) where the extension returns the real type. No
  caller looks at `d_type` for an entry carrying an errno.
* on a filesystem that returns `DT_UNKNOWN` for everything, both backends
  report an entry that vanished between the readdir and the stat as
  vanished; `DirEntry.is_file()` used to swallow that as "not a file" and
  the entry was dropped from the counts entirely. That is a general caveat
  about filesystems with no `d_type`, not a description of any particular
  one: ext4, xfs and the NFSv4 mounts this was measured on all fill it in.
"""

from __future__ import annotations

import os
import stat as _stat


#: The `d_type` values `scan_dir` reports. POSIX names them in <dirent.h>;
#: they are repeated here because the fallback derives them from the stat
#: mode and the scheduler compares against `DT_DIR` per entry.
DT_UNKNOWN = 0
DT_FIFO = 1
DT_CHR = 2
DT_DIR = 4
DT_BLK = 6
DT_REG = 8
DT_LNK = 10
DT_SOCK = 12

_DT_BY_FORMAT = {
    _stat.S_IFIFO: DT_FIFO,
    _stat.S_IFCHR: DT_CHR,
    _stat.S_IFDIR: DT_DIR,
    _stat.S_IFBLK: DT_BLK,
    _stat.S_IFREG: DT_REG,
    _stat.S_IFLNK: DT_LNK,
    _stat.S_IFSOCK: DT_SOCK,
}

_S_IFMT = _stat.S_IFMT

#: (name, d_type, errno) plus seven Nones -- an entry that was not statted,
#: or whose stat failed.
_UNSTATTED = (None, None, None, None, None, None, None)


def _probe_type(entry: os.DirEntry) -> int:
    """The `d_type` of an entry we could not stat, as far as it is knowable."""
    try:
        if entry.is_symlink():
            return DT_LNK
        if entry.is_file(follow_symlinks=False):
            return DT_REG
    except OSError:
        pass
    return DT_UNKNOWN


def scan_dir(fd: int, stat_dirs: bool = False) -> list[tuple]:
    """Read the directory open on `fd`; lstat everything that is not a dir.

    Returns one tuple per entry:
    ``(name, d_type, errno, mode, size, blocks, dev, ino, nlink, mtime)``,
    with the stat fields None when the entry was not statted or the stat
    failed. `fd` stays open and owned by the caller.

    A readdir that fails part way through appends a final entry with an
    empty name carrying that errno, which is how the extension reports the
    same thing and how the scheduler used to see the OSError the `scandir`
    iterator raised: one bad entry, everything before it kept.
    """

    out: list[tuple] = []
    iterator = os.scandir(fd)
    try:
        while True:
            try:
                entry = next(iterator)
            except StopIteration:
                break
            except OSError as exc:
                out.append(("", DT_UNKNOWN, exc.errno or 0, *_UNSTATTED))
                break
            name = entry.name
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                out.append((name, _probe_type(entry), exc.errno or 0, *_UNSTATTED))
                continue
            if is_directory and not stat_dirs:
                out.append((name, DT_DIR, 0, *_UNSTATTED))
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                dtype = DT_DIR if is_directory else _probe_type(entry)
                out.append((name, dtype, exc.errno or 0, *_UNSTATTED))
                continue
            mode = st.st_mode
            out.append((
                name,
                _DT_BY_FORMAT.get(_S_IFMT(mode), DT_UNKNOWN),
                0,
                mode,
                st.st_size,
                # st_blocks is the one field of the ten that a platform is
                # allowed not to have; `walker.make_file_node` guards it the
                # same way, and the scheduler turns a None into "allocated
                # size unavailable" rather than zero.
                getattr(st, "st_blocks", None),
                st.st_dev,
                st.st_ino,
                st.st_nlink,
                st.st_mtime,
            ))
    finally:
        iterator.close()
    return out
