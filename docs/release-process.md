# Release Process

disktide releases use the locked dependency graph, build both standard Python
distribution formats, verify a clean installation, and publish through PyPI
Trusted Publishing. The release workflow is defined in
`.github/workflows/release.yml`.

## Preconditions

1. The release version matches in `pyproject.toml`, `src/disktide/__init__.py`,
   and `uv.lock`.
2. `uv sync --locked` succeeds without changing `uv.lock`.
3. The Python 3.11, 3.12, and 3.13 CI matrix is green.
4. The minimal wheel environment stays within 20 runtime distributions and
   20 MiB, with no native extension.
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
```

## Automated Release

Pushing a `v*` tag runs the release workflow. It:

1. builds and verifies the wheel and sdist;
2. performs a clean-wheel doctor and dependency-budget smoke test;
3. generates SHA-256 checksums and a CycloneDX SBOM;
4. creates GitHub build-provenance attestations;
5. uploads immutable workflow artifacts;
6. publishes wheel and sdist through the protected `pypi` environment.

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

Compare the published wheel checksum with `SHA256SUMS`, retain the SBOM and
attestation links with the release notes, and only then mark the release
complete.
