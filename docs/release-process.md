# Release Process

fsmonitor releases use the locked dependency graph, build both standard Python
distribution formats, verify a clean installation, and publish through PyPI
Trusted Publishing. The release workflow is defined in
`.github/workflows/release.yml`.

## Preconditions

1. The release version matches in `pyproject.toml`, `src/fs_monitor/__init__.py`,
   and `uv.lock`.
2. `uv sync --locked` succeeds without changing `uv.lock`.
3. The Python 3.11, 3.12, and 3.13 CI matrix is green.
4. The minimal wheel environment stays within 20 runtime distributions and
   20 MiB, with no native extension.
5. `fsmonitor doctor`, `fsmonitor doctor --json`, and a small directory scan
   succeed from the built wheel.

## Local Candidate Build

```bash
uv sync --locked
uv run pytest -q
uv build
uv run python tool/verify_distribution.py
```

Refresh the developer's installed checkout after every source version change.
Do not hard-code the expected version; derive it from the package and verify
both entry points:

```bash
expected="$(uv run python -c 'from fs_monitor import __version__; print(__version__)')"
uv tool install --force .
test "$(fsmonitor --version | awk '{print $NF}')" = "$expected"
test "$(fsmonitor-cli --version | awk '{print $NF}')" = "$expected"
uv tool list
```

Install the wheel into a clean environment rather than reusing the development
environment:

```bash
uv venv --python 3.13 .venv-release-smoke
uv pip install --python .venv-release-smoke/bin/python \
  dist/fsmonitor_cli-*.whl
uv venv --python 3.13 .venv-release-sdist
uv pip install --python .venv-release-sdist/bin/python \
  dist/fsmonitor_cli-*.tar.gz
.venv-release-smoke/bin/fsmonitor --version
.venv-release-smoke/bin/fsmonitor doctor --json
.venv-release-sdist/bin/fsmonitor doctor --json
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
uvx fsmonitor-cli --version
uvx fsmonitor-cli doctor
uv tool install --force fsmonitor-cli
fsmonitor --version
pipx install --force fsmonitor-cli
pipx run fsmonitor-cli doctor --json
```

Compare the published wheel checksum with `SHA256SUMS`, retain the SBOM and
attestation links with the release notes, and only then mark the release
complete.
