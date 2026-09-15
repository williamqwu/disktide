"""Docstrings this repo would not be able to compile on a newer Python.

CPython 3.13 cleans every docstring at compile time: to dedent one it
encodes the text to UTF-8. A lone surrogate has no UTF-8 encoding, so a
single one anywhere in a module, class or function docstring makes the
whole file uncompilable --

    UnicodeEncodeError: 'utf-8' codec can't encode character '\\udcff'

-- which under pytest is a *collection* error. The module never runs, the
job stops there, and every job gated on it is skipped. 3.10 through 3.12
compile the same file without complaint, so a dev venv on 3.12 says
nothing and the CI board is the first thing that notices. This gate moves
that discovery back to the machine the file is written on.

The trap is that the offending text reads as documentation. In a non-raw
docstring `"\\udcff"` is not the escape sequence being shown to the
reader, it is the surrogate itself, and the same goes for `b"\\xff"`.
Writing the backslash twice is the fix, and the convention
`disktide.textsafe` already follows.

Only docstrings are cleaned. A `"\\udcff"` *literal* in a test body -- the
undecodable-name tests are full of them, on purpose -- compiles like any
other string and is none of this test's business.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where the fallback listing looks, when there is no git to ask.
SOURCE_TREES = ("src", "tests", "tool")

#: The four node types `ast.get_docstring` accepts, which are also the
#: four the compiler cleans.
DOCSTRING_OWNERS = (
    ast.Module,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)


def python_sources() -> list[Path]:
    """Every tracked `.py` in the repo.

    Asked of git rather than of a glob so that a developer's untracked
    scratch module cannot fail the gate; the glob is the fallback for a
    checkout without git history, such as an unpacked sdist.
    """
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "*.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        found = [REPO_ROOT / "hatch_build.py"]
        for tree in SOURCE_TREES:
            found.extend((REPO_ROOT / tree).rglob("*.py"))
        return sorted(path for path in found if path.is_file())
    return sorted(REPO_ROOT / name for name in listed.split("\0") if name)


def surrogate_docstrings(source: str) -> list[tuple[int, str, str]]:
    """`(line, owner, escapes)` for each docstring holding a lone surrogate.

    The code points come back written as the escapes that should have been
    used, never as themselves: a surrogate in the failure message would
    take the report down the same way it takes the compile down.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, DOCSTRING_OWNERS):
            continue
        text = ast.get_docstring(node, clean=False)
        if text is None:
            continue
        points = sorted({ord(c) for c in text if 0xD800 <= ord(c) <= 0xDFFF})
        if not points:
            continue
        owner = "module" if isinstance(node, ast.Module) else node.name
        escapes = " ".join(f"\\u{point:04x}" for point in points)
        found.append((node.body[0].lineno, owner, escapes))
    return sorted(found)


# --- the gate --------------------------------------------------------------


def test_the_listing_reaches_the_whole_repository():
    """A listing that silently matched nothing would pass the gate below."""
    sources = python_sources()

    assert len(sources) > 200
    assert REPO_ROOT / "src" / "disktide" / "textsafe.py" in sources
    assert REPO_ROOT / "hatch_build.py" in sources


def test_no_docstring_holds_a_lone_surrogate():
    offenders = []
    for path in python_sources():
        text = path.read_text(encoding="utf-8")
        for line, owner, escapes in surrogate_docstrings(text):
            name = path.relative_to(REPO_ROOT)
            offenders.append(f"{name}:{line} ({owner}: {escapes})")

    assert not offenders, (
        "These docstrings contain a lone surrogate:\n  "
        + "\n  ".join(offenders)
        + "\nCPython 3.13 and newer UTF-8-encode every docstring in order "
        "to dedent it, so such a module cannot be compiled at all there -- "
        "pytest fails to collect it and every job behind it is skipped. "
        "Write the backslash twice (`\\\\udcff`, `\\\\xff`) so the reader "
        "still sees the escape, as `disktide.textsafe` does."
    )


# --- the detector, so the gate cannot pass vacuously ------------------------

_SAMPLE = 'def named(path):\n    """One byte on disk: {}."""\n'

#: One backslash: the docstring *is* the surrogate. This is the shape that
#: broke the 3.13 job, and `ast.parse` still reads it on every version.
GUILTY = _SAMPLE.format(r"\udcff")

#: Two: the docstring is seven ASCII characters that read as the escape.
INNOCENT = _SAMPLE.format(r"\\udcff")


def test_the_detector_finds_a_surrogate_in_a_function_docstring():
    assert surrogate_docstrings(GUILTY) == [(2, "named", "\\udcff")]


def test_the_detector_passes_the_doubled_backslash():
    assert surrogate_docstrings(INNOCENT) == []


def test_the_detector_reads_all_four_kinds_of_docstring():
    source = (
        '"""Module \\udcff."""\n'
        "class Holder:\n"
        '    """Class \\udcfe."""\n'
        "    async def method(self):\n"
        '        """Method \\udcfd."""\n'
        "def plain():\n"
        '    """Function \\udcfc."""\n'
    )

    assert surrogate_docstrings(source) == [
        (1, "module", "\\udcff"),
        (3, "Holder", "\\udcfe"),
        (5, "method", "\\udcfd"),
        (7, "plain", "\\udcfc"),
    ]


def test_the_detector_leaves_string_literals_alone():
    """The literals in `test_undecodable_names` are correct and must stay."""
    assert surrogate_docstrings('name = "\\udcff"\n') == []
    assert surrogate_docstrings("def f():\n    return '\\udcff'\n") == []
