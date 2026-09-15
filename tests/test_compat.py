"""Tests for the 3.10 standard-library shims.

The 3.10 StrEnum backport is only exercised on 3.10, but its contract is
asserted on every version: these tests run against whichever implementation
the interpreter supplies, so a drift between the backport and the stdlib
shows up as a failure on the 3.10 job rather than as odd rendering later.
"""

import enum
import json
import sys
from enum import auto

from disktide._compat import StrEnum, tomllib
from disktide.domain.scan import ScanStatus


class Sample(StrEnum):
    ALPHA = "alpha"
    DASHED = "with-dash"


class Generated(StrEnum):
    LOWER_ME = auto()


class TestStrEnum:
    def test_shim_only_below_311(self):
        if sys.version_info >= (3, 11):
            assert StrEnum is enum.StrEnum
        else:
            assert StrEnum is not getattr(enum, "StrEnum", None)

    def test_str_is_the_bare_value(self):
        assert str(Sample.ALPHA) == "alpha"
        assert str(Sample.DASHED) == "with-dash"

    def test_format_is_the_bare_value(self):
        assert f"{Sample.ALPHA}" == "alpha"
        assert format(Sample.DASHED) == "with-dash"
        # A format spec applies to the value, not to the repr.
        assert f"{Sample.ALPHA:>7}" == "  alpha"

    def test_json_serializes_as_the_value(self):
        assert json.dumps(Sample.ALPHA) == json.dumps("alpha")
        assert json.dumps({"k": Sample.DASHED}) == json.dumps({"k": "with-dash"})

    def test_compares_and_hashes_as_str(self):
        assert Sample.ALPHA == "alpha"
        assert isinstance(Sample.ALPHA, str)
        assert {"alpha": 1}[Sample.ALPHA] == 1

    def test_auto_lowercases_the_member_name(self):
        assert Generated.LOWER_ME.value == "lower_me"

    def test_project_enum_round_trips(self):
        # The domain enums are persisted and rendered as bare strings.
        assert str(ScanStatus.COMPLETED) == "completed"
        assert ScanStatus("completed") is ScanStatus.COMPLETED


class TestTomllib:
    def test_parses_toml(self):
        assert tomllib.loads('key = "value"') == {"key": "value"}

    def test_exposes_decode_error(self):
        assert issubclass(tomllib.TOMLDecodeError, Exception)
        try:
            tomllib.loads("not = = toml")
        except tomllib.TOMLDecodeError:
            pass
        else:  # pragma: no cover - only reached on a broken parser
            raise AssertionError("expected TOMLDecodeError")
