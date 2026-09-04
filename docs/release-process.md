# Release Process

disktide releases use the locked dependency graph, build both standard Python
distribution formats, verify a clean installation, and publish through PyPI
Trusted Publishing. The release workflow is defined in
`.github/workflows/release.yml`.

Since the scanner grew an optional C extension the artifact set is a **wheel
matrix plus the sdist**, not one pure wheel plus the sdist. See *The wheel
matrix* below for what is built and why the `py3-none-any` wheel is no longer
published.

## Preconditions

1. The release version matches in `pyproject.toml`, `src/disktide/__init__.py`,
   and `uv.lock`.
2. `uv sync --locked` succeeds without changing `uv.lock`.
3. The Python 3.10, 3.11, 3.12, 3.13, and 3.14 CI matrix is green.
4. The minimal wheel environment stays within 20 runtime distributions and
   20 MiB, with no *third-party* native extension. disktide's own optional
   scanner accelerator is exempt and is reported separately by
   `tool/check_dependency_budget.py`.
5. `disktide doctor`, `disktide doctor --json`, a small directory scan, and a
   periodic watch smoke succeed from the core wheel.
6. A separate clean install of `disktide[watch]` discovers the native
   backend and completes strict `watch --events` smoke.
7. The full test suite passes in both the locked core environment and a locked
   Python 3.13 environment with the `watch` extra installed.

## Local Candidate Build

```bash
uv sync --locked
.venv/bin/python -m pytest -q
uv sync --locked --python 3.13 --extra watch
.venv/bin/python -m pytest -q
uv sync --locked
uv build
.venv/bin/python tool/verify_distribution.py
```

Refresh the developer's installed checkout after every source version change.
Do not hard-code the expected version; derive it through the compatibility
namespace required by the local delivery workflow and verify all entry points:

```bash
expected="$(uv run python -c 'from fs_monitor import __version__; print(__version__)')"
uv tool install --force .
test "$(disktide --version | awk '{print $NF}')" = "$expected"
test "$(sizetrail --version | awk '{print $NF}')" = "$expected"
test "$(fsmonitor --version | awk '{print $NF}')" = "$expected"
test "$(fsmonitor-cli --version | awk '{print $NF}')" = "$expected"
uv tool list
```

Install the wheel into a clean environment rather than reusing the development
environment:

```bash
uv venv --python 3.13 .venv-release-smoke
uv pip install --python .venv-release-smoke/bin/python \
  dist/disktide-*.whl
uv venv --python 3.13 .venv-release-sdist
uv pip install --python .venv-release-sdist/bin/python \
  dist/disktide-*.tar.gz
uv venv --python 3.13 .venv-release-watch
wheel=$(echo dist/disktide-*.whl)
uv pip install --python .venv-release-watch/bin/python \
  "disktide[watch] @ file://${PWD}/${wheel}"
mkdir -p /tmp/disktide-smoke
printf 'smoke' > /tmp/disktide-smoke/payload
.venv-release-smoke/bin/disktide --version
.venv-release-smoke/bin/disktide doctor --json
.venv-release-smoke/bin/disktide watch /tmp/disktide-smoke \
  --periodic-only --interval 1h --max-time 1s
.venv-release-sdist/bin/disktide doctor --json
.venv-release-watch/bin/disktide doctor --json
.venv-release-watch/bin/disktide watch /tmp/disktide-smoke \
  --events --interval 1h --max-time 1s
.venv-release-smoke/bin/python tool/check_dependency_budget.py
.venv-release-smoke/bin/disktide doctor | grep Scanner
```

A local `uv build` produces a `linux_x86_64` (or `macosx_*`) wheel, which is
right for this smoke test and wrong for PyPI -- the published wheels come
from the release workflow's `cibuildwheel` matrix. To rehearse the
compiler-less path locally:

```bash
CC=/bin/false uv build --wheel --out-dir dist-pure   # -> py3-none-any
uv venv --python 3.13 .venv-release-pure
CC=/bin/false uv pip install --no-cache \
  --python .venv-release-pure/bin/python dist/disktide-*.tar.gz
.venv-release-pure/bin/disktide doctor | grep Scanner   # python fallback
```

## The wheel matrix

`disktide/scanner/_scanfast` is compiled per interpreter and per platform, so
one wheel cannot serve everyone. The `wheels` job runs `cibuildwheel` for
CPython 3.10 through 3.14 on:

| Runner | Wheels |
|--------|--------|
| `ubuntu-latest` | `manylinux_x86_64`, `musllinux_x86_64` |
| `ubuntu-latest` + QEMU | `manylinux_aarch64`, `musllinux_aarch64` |
| `macos-14` | `macosx_arm64` |
| `macos-13` | `macosx_x86_64` |

Every wheel is tested inside the job by importing
`disktide.scanner.accel.ACCEL_BACKEND` and asserting `native`: the build hook
is deliberately forgiving about a missing compiler, and without that check a
broken container would publish a platform-tagged wheel with nothing
platform-specific in it. The `linux_x86_64` tag the hook produces is not
acceptable to PyPI; cibuildwheel's `auditwheel repair` step is what turns it
into `manylinux`. The extension links libc and nothing else, so there is
nothing for it to vendor.

**The `py3-none-any` wheel is no longer published.** A version ships either
one pure wheel or a set of platform wheels, and the sdist is what every other
platform installs: it rebuilds the extension where there is a compiler and
runs the pure-Python fallback where there is not. The release job installs
the sdist both ways — plain, and with `CC=/bin/false` — and asserts the
backend each install reports, so the fallback path cannot rot unnoticed.

The aarch64 legs run under binfmt emulation, which is where most of the extra
release CI time goes (roughly ten minutes for the matrix). A repository with
`ubuntu-24.04-arm` runners available should switch to them and drop the QEMU
step.

## Automated Release

Pushing a `v*` tag runs the release workflow. It:

1. builds the platform wheels (the matrix above), each tested for the native
   backend, and the sdist;
2. verifies every wheel and the sdist with `tool/verify_distribution.py`;
3. performs a clean-install doctor and dependency-budget smoke test, and
   asserts the scanner backend of three installs: the platform wheel, the
   sdist with a compiler, the sdist without one;
4. generates SHA-256 checksums and a CycloneDX SBOM;
5. creates GitHub build-provenance attestations, on a public repository
   only -- attestations require Enterprise Cloud on a private one, so the
   step is guarded by repository visibility and skips itself rather than
   failing the release;
6. uploads immutable workflow artifacts;
7. publishes every wheel and the sdist through the protected `pypi`
   environment.

The repository must configure PyPI Trusted Publishing for the `release.yml`
workflow and protect the `pypi` environment before the first tag is pushed.

## Post-Publish Verification

After PyPI has indexed the release, verify each supported installer in a clean
shell:

```bash
uvx disktide --version
uvx disktide doctor
uv tool install --force disktide
disktide --version
pipx install --force disktide
pipx run disktide doctor --json
```

Confirm that `uvx disktide doctor` reports `Scanner: native (_scanfast)` on
a platform the matrix covers -- that is the end-to-end proof the right wheel
was selected. Compare the published wheel checksums with `SHA256SUMS`, retain the SBOM and,
where the repository was public at build time, the attestation links with the
release notes, and only then mark the release complete.
