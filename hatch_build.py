"""Compile the optional scanner extension for wheel builds; stay pure when we cannot.

`disktide/scanner/_scanfast` reads a whole directory and lstats its entries
inside one GIL release. It is an accelerator and nothing else: every install
that does not get it runs `_scanfast_py` instead and produces the same trees,
so this hook never fails a build. No compiler, an unknown compiler, a
compile that errors -- each of those leaves a `py3-none-any` wheel behind and
says so in the build log.

Two things this hook is careful about, both learned the hard way:

1. The `.so` is compiled into a temporary directory, **never** into `src/`.
   A `.so` left under the package directory is an ordinary file to the next
   build, and a *pure* build swept it into a `py3-none-any` wheel that
   secretly carried an x86_64 binary. `exclude = ["*.so", "*.pyd",
   "*.dylib"]` on the wheel target is the belt to this hook's braces --
   developers do build in place. The temporary directory is not `dist/`
   either: the release job checksums everything in there.
2. The compiler is looked for by name before it is run. `sysconfig`'s `CC`
   is whatever built the interpreter, which on a uv-managed CPython is
   `clang` -- a name most Linux release runners do not have. `$CC`, then
   sysconfig's, then plain `cc`, `gcc`, `clang`: the first one on `PATH`
   wins.

And one thing it checks before it commits to a platform wheel: that the
object it just built actually loads. A compiler that *succeeds* and produces
something the interpreter cannot import -- a wrong ABI, a stale `Python.h`,
a `$CC` that is not really a C compiler for this interpreter -- used to
produce a platform-tagged wheel with a dead `.so` in it, and the only symptom
was `disktide doctor` quietly reporting the Python fallback. The release
matrix's `CIBW_TEST_COMMAND` catches that for published wheels; nothing
caught it for an sdist install or a local build.

And one platform difference: a CPython extension on macOS is a *bundle*
(`-bundle -undefined dynamic_lookup`), not a shared library. `-shared` there
fails to link, because the Python symbols the module calls are resolved by
the interpreter that loads it and are in no library at build time.

No setuptools: it would be a second build requirement for a hundred lines of
compiler invocation, and this project ships `hatchling` alone.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


#: In preference order. `$CC` first so a cross-compile or a cibuildwheel
#: container can name its own, sysconfig's next because it matches the
#: interpreter's ABI where it exists at all.
def _compiler_candidates() -> list[str]:
    candidates = [
        os.environ.get("CC"),
        sysconfig.get_config_var("CC"),
        "cc",
        "gcc",
        "clang",
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def _resolve_compiler() -> list[str] | None:
    """The first candidate whose executable is actually on PATH."""
    for candidate in _compiler_candidates():
        argv = candidate.split()
        if argv and shutil.which(argv[0]) is not None:
            return argv
    return None


def _cross_architecture(archflags: list[str]) -> str | None:
    """The architecture `ARCHFLAGS` asks for, when it is not this machine's.

    cibuildwheel cross-compiles on macOS by putting `-arch arm64` (or
    x86_64) in `ARCHFLAGS`, and an object built for the other architecture
    cannot be loaded here however healthy it is. Returns the foreign
    architecture so the caller can say which check it skipped and why.
    """
    wanted = [
        archflags[index + 1]
        for index, flag in enumerate(archflags)
        if flag == "-arch" and index + 1 < len(archflags)
    ]
    host = platform.machine()
    foreign = [arch for arch in wanted if arch != host]
    return foreign[0] if foreign else None


def _import_failure(path: str) -> str | None:
    """Why the freshly built object would not load, or None if it does.

    In a subprocess, because an extension loaded into this interpreter
    cannot be unloaded again and this hook has more work to do; and by file
    location rather than by name, because `disktide` is not importable in a
    build environment -- the package is not installed anywhere yet, and the
    point is to test the object, not the package around it.
    """
    script = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('_scanfast', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "module.scan_dir\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, path],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if result.returncode == 0:
        return None
    lines = (result.stderr or "").strip().splitlines()
    return lines[-1] if lines else f"exit status {result.returncode}"


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    _scratch: str | None = None

    def initialize(self, version, build_data):
        if self.target_name != "wheel":
            return
        if os.environ.get("DISKTIDE_NO_EXTENSION"):
            self.app.display_warning(
                "scanfast: DISKTIDE_NO_EXTENSION set, building a pure-Python wheel"
            )
            return

        source = os.path.join(
            self.root, "src", "disktide", "scanner", "_scanfast.c"
        )
        if not os.path.exists(source):
            self.app.display_warning(
                f"scanfast: {source} is missing, building a pure-Python wheel"
            )
            return

        compiler = _resolve_compiler()
        if compiler is None:
            self.app.display_warning(
                "scanfast: no C compiler on PATH, building a pure-Python wheel"
            )
            return

        suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
        self._scratch = tempfile.mkdtemp(prefix="disktide-scanfast-")
        output = os.path.join(self._scratch, "_scanfast" + suffix)
        include = sysconfig.get_paths()["include"]
        # cibuildwheel sets ARCHFLAGS when it cross-compiles on macOS; a
        # build that ignored it would put the host's architecture in a wheel
        # tagged for another one.
        archflags = shlex.split(os.environ.get("ARCHFLAGS", ""))
        if sys.platform == "darwin":
            link = ["-bundle", "-undefined", "dynamic_lookup"]
        else:
            link = ["-shared"]
        command = [
            *compiler,
            "-O2",
            "-Wall",
            *link,
            "-fPIC",
            *archflags,
            "-I" + include,
            "-o",
            output,
            source,
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            self.app.display_warning(
                f"scanfast: {compiler[0]} failed ({exc.stderr.strip()}); "
                "building a pure-Python wheel"
            )
            return
        except OSError as exc:
            self.app.display_warning(
                f"scanfast: {compiler[0]} could not be run ({exc}); "
                "building a pure-Python wheel"
            )
            return

        cross = _cross_architecture(archflags)
        if cross is not None:
            # Nothing here can load an arm64 object on x86_64, or the other
            # way round. The release matrix's CIBW_TEST_COMMAND runs on the
            # target, which is where that wheel gets checked.
            self.app.display_warning(
                f"scanfast: ARCHFLAGS builds for {cross} and this is "
                f"{platform.machine()}, so the object was not loaded here"
            )
        else:
            failure = _import_failure(output)
            if failure is not None:
                self.app.display_warning(
                    f"scanfast: {compiler[0]} succeeded but the object it "
                    f"produced does not import ({failure}); building a "
                    "pure-Python wheel"
                )
                return

        build_data["pure_python"] = False
        build_data["infer_tag"] = True
        build_data["force_include"][output] = (
            "disktide/scanner/_scanfast" + suffix
        )
        self.app.display_info(
            f"scanfast: built _scanfast{suffix} with {compiler[0]}"
        )

    def finalize(self, version, build_data, artifact_path):
        # The wheel has been written by now; the object file has done its job.
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None
