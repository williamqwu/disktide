"""Standard-library shims for the oldest supported interpreter.

disktide supports Python 3.10, which predates two stdlib additions the code
base would otherwise use directly: ``tomllib`` (3.11) and ``enum.StrEnum``
(3.11).  Importing them from here keeps the call sites identical across every
supported version, so the rest of the tree never grows version checks.

On 3.11+ both names are the genuine stdlib objects and this module costs one
extra import.  Drop the module -- and the conditional ``tomli`` dependency in
pyproject.toml -- when the floor moves to 3.11.
"""

from __future__ import annotations

import sys

try:  # pragma: no cover - the branch taken depends on the interpreter
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    # tomli is the upstream of stdlib tomllib and exposes the same surface,
    # including TOMLDecodeError, so callers need no further branching.
    import tomli as tomllib

if sys.version_info >= (3, 11):  # pragma: no cover - version dependent
    from enum import StrEnum
else:  # pragma: no cover - Python 3.10 only
    from enum import Enum

    class StrEnum(str, Enum):
        """Backport of 3.11's ``enum.StrEnum``.

        3.11 defines ``StrEnum`` as ``str`` mixed with ``ReprEnum``, which
        leaves ``__str__`` and ``__format__`` to ``str`` so a member renders
        as its bare value rather than ``ClassName.MEMBER``.  3.10's plain
        ``str, Enum`` mixin does not, so both are restored explicitly to keep
        f-strings, logging, and TOML/JSON round-trips byte-identical across
        versions.
        """

        def __str__(self) -> str:
            return str(self.value)

        def __format__(self, format_spec: str) -> str:
            # Format the underlying string, not the repr, matching 3.11.
            return str.__format__(self, format_spec)

        @staticmethod
        def _generate_next_value_(
            name: str, start: int, count: int, last_values: list[str]
        ) -> str:
            # 3.11 lowercases the member name for auto() values.
            return name.lower()


__all__ = ["StrEnum", "tomllib"]
