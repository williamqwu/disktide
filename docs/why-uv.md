# Why uv?

DiskTide's install instructions lead with [uv](https://docs.astral.sh/uv/).
Here is what it is, why it is recommended, and how to get it.

## What uv is

uv is a Python package and project manager from Astral. It is a single binary,
written in Rust, and it covers most of what people use pip, venv, pipx and
pyenv for. It can also download a Python interpreter for you, so it works on a
machine with no suitable Python installed.

## Why DiskTide recommends it

`uv tool install .` builds the package once, installs it into its own isolated
environment, and puts a `disktide` command on your PATH. That means:

- Nothing touches your system Python or any project virtualenv.
- There is no environment to activate. You type `disktide` and it runs.
- `uv tool upgrade` and `uv tool uninstall` manage it afterwards.
- Resolving and installing is much faster than pip.

Once DiskTide is published to PyPI, `uvx disktide` will run it without
installing anything at all.

## Installing uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # Linux and macOS
brew install uv                                   # macOS with Homebrew
pipx install uv                                   # if you already have Python
pip install uv                                    # same, without pipx
```

On Windows (PowerShell), the uv install line is below. Note that DiskTide
itself does not support Windows yet, so this only gets you uv:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Check it with `uv --version`. If the `disktide` command is not found after
`uv tool install`, run `uv tool update-shell` and open a new shell. uv puts its
shims in `~/.local/bin`.

## The commands you need

```bash
uv tool install .              # install from a checkout
uv tool install --reinstall .  # after a git pull
uv tool install '.[watch]'     # with the Linux inotify extra
uv tool list                   # what is installed
uv tool uninstall disktide     # remove it
```

## Not a requirement

pip still works. See the [README](../README.md) for the pip flow. uv is a
recommendation, not a requirement. The full uv documentation lives at
<https://docs.astral.sh/uv/>.
