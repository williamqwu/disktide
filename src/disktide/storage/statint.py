"""Storing `st_ino` and `st_dev` in a column SQLite calls INTEGER.

A SQLite INTEGER is a signed 64-bit number. An inode number is not: `ino_t`
is unsigned on every platform Python runs on, and some filesystems use the
whole width. A NetApp export hands out inode numbers around 9.2e18, past
2**63 - 1, and `sqlite3` refuses them at the INSERT with ``OverflowError:
Python int too large to convert to SQLite INTEGER``. So every `scan -s` on
such a share scanned the whole tree and then saved nothing, and every
monitor run on it failed at `record_monitor_run` the same way.

The column stays INTEGER and the value is folded into it two's-complement
style: anything at or above 2**63 is stored as the negative number with the
same low 64 bits, and a negative number read back is the unsigned value it
stood for. Equality survives the trip -- which is the only thing these
columns are used for; the root identity check in `services.compare` asks
whether two snapshots saw the same (device, inode) and never orders them.

Not TEXT, which looks like the obvious alternative. The columns already
exist with INTEGER affinity, and under that affinity a text value that
parses as an integer too wide for 64 signed bits is *converted to REAL*
(SQLite datatype docs, section 3.1), which keeps about 16 of the 19 digits.
Two distinct inodes on the same share would then compare equal.

Values already in range are stored unchanged, so nothing written before
this module existed reads back differently -- a wider value could never
have been written at all.
"""

from __future__ import annotations

__all__ = ["load_stat_int", "store_stat_int"]

_SIGNED_MAX = 2**63 - 1
_UNSIGNED_MODULUS = 2**64


def store_stat_int(value: int | None) -> int | None:
    """Fold an unsigned 64-bit stat field into SQLite's signed INTEGER."""
    if value is None:
        return None
    if value > _SIGNED_MAX:
        if value >= _UNSIGNED_MODULUS:
            raise ValueError(
                f"stat field {value} does not fit in 64 bits; no platform "
                "produces one this wide"
            )
        return value - _UNSIGNED_MODULUS
    return value


def load_stat_int(value: int | None) -> int | None:
    """Undo `store_stat_int`: a negative column value is an unsigned inode."""
    if value is None:
        return None
    if value < 0:
        return value + _UNSIGNED_MODULUS
    return value
