"""How many colours the terminal can actually paint, and how we know.

The chart is drawn in 24-bit RGB and the terminal decides what happens to
it on the way out.  That decision is invisible from inside the process --
Rich picks a colour system at startup, writes the matching escape codes,
and nothing ever reports back -- so a session can spend its whole life
emitting 256-colour SGRs to a client that quantises every one of them onto
sixteen slots, and the only symptom is a picture whose colours are wrong.

That is not a corner case.  Open OnDemand's shell app spawns its pty with
``TERM=xterm-16color`` and JupyterLab's terminal with ``TERM=xterm-color``;
Rich reads sixteen colours out of both.  Inside tmux the reading is worse
than wrong, it is *stale*: the pty tmux gives the app says
``tmux-256color`` whatever the attached client is, so the app writes
256-colour codes and tmux maps each one through its own static
``colour_256to16`` table before the browser ever sees it.  On the shipped
``disktide`` palette that collapses 39 distinct colours to 30 at 256 and
to 13 at sixteen -- ``archive``, ``ephemeral`` and the neutral directory
ring all land on ANSI 1, and ``data`` lands on the same index as the
window border.

So the depth is resolved in layers, most authoritative first, and each
answer carries where it came from so ``disktide doctor`` can explain the
picture rather than leaving a user to guess:

  ``env``          ``DISKTIDE_COLOR_DEPTH``, for scripts and one-off shells.
  ``config``       ``[ui] color_depth``, the answer a user keeps.
  ``textual-env``  ``TEXTUAL_COLOR_SYSTEM``, if the user set it themselves.
                   Never overwritten: it is Textual's own knob and someone
                   who has reached for it has already decided.
  ``tmux-client``  what the clients attached to *this* tmux session say
                   they can do, weakest first.
  ``colorterm``    ``COLORTERM=truecolor|24bit``, the de-facto RGB flag,
                   for the sessions the layer above could not answer.
  ``term``         ``TERM``, which is a promise about terminfo rather than
                   a measurement, and the only thing left in a web shell.
  ``default``      256, which is what shipped before any of this existed.

The tmux layer is the one that matters and the one nothing else can do.
``TERM`` inside tmux describes the *pty*, so it is useless; the attached
clients are the only place the truth is written down, and a session can
have several.  The least capable of them governs, because a session
attached from both a laptop and a web shell has to be legible in the web
shell -- the laptop can read sixteen colours, the browser cannot read 256.

Which is also why it sits *above* ``COLORTERM`` and not below it.
``COLORTERM`` is a property of a process's environment, not of a pane:
inside tmux it is whatever the shell that started the server exported, or
whatever the user's rc sets, and it survives every detach and reattach.
It therefore says nothing at all about the client currently looking at
this pane -- and in the case this module was written for it says the
opposite of the truth.  One server attached from a laptop
(``xterm-256color``, RGB) and from an Open OnDemand web shell
(``xterm-16color``) with ``COLORTERM=truecolor`` in the rc would answer
truecolor in the browser, write 24-bit SGRs, and hand tmux exactly the
quantisation the ANSI theme exists to avoid.  So ``COLORTERM`` is
consulted outside tmux, and inside it only when tmux could not be asked:
no server, a wedged one, a query that timed out, or a session with no
clients attached.  The three explicit overrides stay above both, because
someone who typed a depth in has already decided.

Stdlib only and no Textual import, like ``cellgeom``: this has to run
*before* ``textual.constants`` is imported, since that module reads
``TEXTUAL_COLOR_SYSTEM`` once at import time and never looks again.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping

#: The three answers.  Ordered most capable first; `_RANK` below is what
#: makes "the weakest attached client wins" a comparison rather than a
#: pile of conditionals.
COLOR_DEPTHS: tuple[str, ...] = ("truecolor", "256", "16")

ColorDepthValue = Literal["truecolor", "256", "16"]

_RANK: dict[str, int] = {value: index for index, value in enumerate(COLOR_DEPTHS)}

#: Environment override, e.g. ``DISKTIDE_COLOR_DEPTH=truecolor``.
DEPTH_ENV_VAR = "DISKTIDE_COLOR_DEPTH"

#: Textual's own override, which this module reads and may set but never
#: overwrites.
TEXTUAL_ENV_VAR = "TEXTUAL_COLOR_SYSTEM"

#: What ``[ui] color_depth`` holds when it is not answering.
AUTO = "auto"

#: What nothing at all resolves to, which is what shipped before this
#: module existed.
DEFAULT_COLOR_DEPTH: ColorDepthValue = "256"

#: Hard ceiling on the tmux interrogation.  A running server answers in
#: about a millisecond; one that is wedged, or a `tmux` on a stale NFS
#: mount, must not hold up a launch.
TMUX_TIMEOUT = 0.5

#: Depth -> the string ``textual.constants.COLOR_SYSTEM`` is set to, which
#: Textual hands straight to ``rich.console.Console(color_system=...)``.
#: The values are Rich's own ``COLOR_SYSTEMS`` keys -- ``256``, not
#: ``eightbit``, which Rich would reject.
TEXTUAL_COLOR_SYSTEMS: dict[str, str] = {
    "truecolor": "truecolor",
    "256": "256",
    "16": "standard",
}

# What a user-set TEXTUAL_COLOR_SYSTEM already decided.  Both Rich's names
# and the spellings people actually type are accepted; `auto` is not an
# answer, so it falls through to the layers below.
_TEXTUAL_SYSTEM_DEPTH: dict[str, str] = {
    "truecolor": "truecolor",
    "24bit": "truecolor",
    "256": "256",
    "eightbit": "256",
    "eight_bit": "256",
    "standard": "16",
    "ansi": "16",
    # The legacy Windows console has sixteen colours and no more.
    "windows": "16",
}

# Spellings accepted from `DISKTIDE_COLOR_DEPTH` and `[ui] color_depth`.
_DEPTH_ALIASES: dict[str, str] = {
    "truecolor": "truecolor",
    "true-color": "truecolor",
    "24bit": "truecolor",
    "24-bit": "truecolor",
    "rgb": "truecolor",
    "256": "256",
    "8bit": "256",
    "eightbit": "256",
    "16": "16",
    "standard": "16",
    "ansi": "16",
}

# Terminals that support RGB and say so nowhere else.  A `TERM` containing
# `direct` is the terminfo convention for a direct-colour entry and covers
# `xterm-direct`, `tmux-direct` and friends without listing them.
_TRUECOLOR_TERMS: frozenset[str] = frozenset({
    "xterm-kitty", "alacritty", "wezterm", "foot", "contour", "rio",
    "ghostty",
})

# The reading a `TERM` that promises sixteen deserves, spelled out because
# it is wrong far more often than it is right: every xterm.js-based web
# shell can do 256 and RGB and simply never says so.
_SIXTEEN_DETAIL = (
    "TERM promises only 16; xterm.js-based shells (OnDemand, Jupyter) "
    "actually support 256/RGB — see doctor"
)


@dataclass(frozen=True, slots=True)
class ColorDepth:
    """A resolved colour depth and the evidence behind it.

    Callers that only render want ``value``; the ones that have to explain
    themselves to a user -- ``doctor``, the toast the app raises when it
    swaps to the ANSI theme -- need to say which layer answered and what
    it saw, because that is the half a user can act on.
    """

    value: ColorDepthValue
    source: str
    detail: str = ""

    @property
    def measured(self) -> bool:
        """Whether something reported this, rather than it being assumed
        or typed in by hand."""
        return self.source in ("colorterm", "tmux-client", "term")


@dataclass(frozen=True, slots=True)
class TmuxClient:
    """One client attached to a tmux session, as tmux describes it."""

    termname: str
    features: tuple[str, ...]

    @property
    def depth(self) -> ColorDepthValue:
        """What this client can paint.

        ``client_termfeatures`` is tmux's own resolved answer -- terminfo
        plus whatever ``terminal-features`` adds -- so ``RGB`` and ``256``
        there are authoritative.  The termname is only consulted when tmux
        listed no features at all, which is what an ancient server does.
        """
        if "RGB" in self.features or "Tc" in self.features:
            return "truecolor"
        if "256" in self.features:
            return "256"
        name = self.termname.lower()
        if "direct" in name:
            return "truecolor"
        if "256color" in name:
            return "256"
        return "16"

    def describe(self) -> str:
        features = ",".join(self.features) if self.features else "none"
        return f"{self.termname or 'unknown'}: {features}"


# Process-global, written once at launch by `disktide.__main__` before the
# app is imported and read from there on by the app and by doctor.  Same
# shape as `cellgeom`'s module state and for the same reason: the answer
# has to be identical everywhere in a session, and the layers it comes
# from include a subprocess nobody wants to run twice.
_active: ColorDepth | None = None


def set_active_color_depth(depth: ColorDepth) -> None:
    """Install the depth this session runs at."""
    global _active
    _active = depth


def active_color_depth() -> ColorDepth:
    """The depth this session runs at, resolving one if nothing installed it.

    The lazy path is for callers that never went through the CLI -- tests,
    and anything embedding the app -- and it caches, so the tmux
    interrogation happens at most once per process.
    """
    global _active
    if _active is None:
        _active = resolve_color_depth()
    return _active


def reset_color_depth() -> None:
    """Forget the installed depth.

    Exists for the test suite: this is process-global, so one test that
    pins a depth would otherwise decide the colours of every test after it.
    """
    global _active
    _active = None


def normalize_depth(value: object) -> ColorDepthValue | None:
    """The depth *value* names, or None for "not an answer".

    Anything unrecognised -- a typo, ``auto``, an empty string, a bool from
    a TOML file -- reads as unset rather than raising, because the fallback
    is the detection this exists to override and refusing to start over a
    misspelling would be far worse than ignoring it.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().lower()
    if not text or text == AUTO:
        return None
    return _DEPTH_ALIASES.get(text)  # type: ignore[return-value]


def config_color_depth(path: "os.PathLike[str] | str | None" = None) -> str | None:
    """``[ui] color_depth`` read straight out of the config file.

    Deliberately not through ``disktide.config``: that module reaches
    ``disktide.keys``, which imports Textual, and ``textual.constants``
    reads ``TEXTUAL_COLOR_SYSTEM`` once at its own import. So a launch that
    loaded the config first to find out what depth to ask for would have
    already lost the ability to ask -- the variable would be set and
    nothing would be reading it any more. This layer therefore reads the
    one key it needs, before anything else is imported at all.

    Every failure is None: a missing file, an unreadable one, malformed
    TOML, a value that is not a depth. What that costs is the config
    layer, and what is left is the detection this key exists to override.
    """
    try:
        from disktide._compat import tomllib
        from disktide.paths import config_file

        target = Path(config_file() if path is None else path).expanduser()
        with open(target, "rb") as handle:
            data = tomllib.load(handle)
        ui = data.get("ui")
        if not isinstance(ui, dict):
            return None
        return normalize_depth(ui.get("color_depth"))
    except Exception:
        return None


def _run_tmux(args: list[str]) -> str | None:
    """Ask the running tmux server something, or None if it cannot be asked.

    Every failure is the same answer -- no tmux, no server, a wedged one,
    a `tmux` binary that is not on the path -- because the caller does the
    same thing with all of them: skip the layer and read `TERM` instead.
    """
    try:
        completed = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=TMUX_TIMEOUT,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


TmuxRunner = Callable[[list[str]], "str | None"]


def tmux_clients(runner: TmuxRunner | None = None) -> tuple[TmuxClient, ...]:
    """Every client attached to *this* tmux session.

    Scoped to the session on purpose.  A server can host several, and the
    colours this app gets are decided by the clients looking at *its*
    session -- somebody else's detached window has no vote.
    """
    run = _run_tmux if runner is None else runner
    raw_session = run(["display", "-p", "#S"])
    if not raw_session:
        return ()
    session = raw_session.strip().splitlines()[0].strip() if raw_session.strip() else ""
    if not session:
        return ()
    listing = run([
        "list-clients", "-t", session,
        "-F", "#{client_termname}\t#{client_termfeatures}",
    ])
    if not listing:
        return ()
    clients: list[TmuxClient] = []
    for line in listing.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        termname, _, features = line.partition("\t")
        clients.append(TmuxClient(
            termname=termname.strip(),
            features=tuple(
                part for part in features.strip().split(",") if part
            ),
        ))
    return tuple(clients)


def _weakest(clients: tuple[TmuxClient, ...]) -> TmuxClient:
    """The attached client with the least colour, which is the one that
    decides: a session read in a browser and on a laptop at once has to be
    legible in the browser."""
    return max(clients, key=lambda client: _RANK[client.depth])


def _term_depth(term: str) -> tuple[ColorDepthValue, str]:
    """What `TERM` promises, and how to read the promise."""
    lowered = term.lower()
    if "direct" in lowered or lowered in _TRUECOLOR_TERMS:
        return "truecolor", f"TERM={term} is a direct-colour terminal"
    if "256color" in lowered:
        return "256", f"TERM={term}"
    return "16", f"TERM={term}: {_SIXTEEN_DETAIL}"


def resolve_color_depth(
    *,
    config_depth: object = None,
    environ: Mapping[str, str] | None = None,
    runner: TmuxRunner | None = None,
) -> ColorDepth:
    """How many colours to paint with, with its provenance.

    Layered exactly as the module docstring lists.  ``environ`` and
    ``runner`` are injected rather than read from the process so each
    layer can be tested on its own -- a resolver whose only test is "what
    does this developer's shell say" is a resolver with no test at all.
    """
    env = os.environ if environ is None else environ
    try:
        raw_env = env.get(DEPTH_ENV_VAR)
        chosen = normalize_depth(raw_env)
        if chosen is not None:
            return ColorDepth(chosen, "env", f"{DEPTH_ENV_VAR}={raw_env}")

        chosen = normalize_depth(config_depth)
        if chosen is not None:
            return ColorDepth(
                chosen, "config", f"[ui] color_depth = {config_depth!r}"
            )

        raw_textual = (env.get(TEXTUAL_ENV_VAR) or "").strip().lower()
        textual_depth = _TEXTUAL_SYSTEM_DEPTH.get(raw_textual)
        if textual_depth is not None:
            return ColorDepth(
                textual_depth,  # type: ignore[arg-type]
                "textual-env",
                f"{TEXTUAL_ENV_VAR}={raw_textual}",
            )

        # Above COLORTERM on purpose -- see the module docstring. What a
        # client can paint is a fact about the client; COLORTERM is a fact
        # about an environment that outlives every attach.
        if env.get("TMUX"):
            clients = tmux_clients(runner)
            if clients:
                weakest = _weakest(clients)
                if len(clients) == 1:
                    detail = f"attached client {weakest.describe()}"
                else:
                    detail = (
                        f"weakest of {len(clients)} attached clients, "
                        f"{weakest.describe()}"
                    )
                return ColorDepth(weakest.depth, "tmux-client", detail)

        colorterm = (env.get("COLORTERM") or "").strip().lower()
        if colorterm in ("truecolor", "24bit"):
            return ColorDepth(
                "truecolor", "colorterm", f"COLORTERM={colorterm}"
            )

        term = (env.get("TERM") or "").strip()
        if term and term.lower() != "dumb":
            value, detail = _term_depth(term)
            return ColorDepth(value, "term", detail)
    except Exception:
        # A depth that is merely conservative costs a user some hues; an
        # exception here costs them the session.
        return ColorDepth(DEFAULT_COLOR_DEPTH, "default", "detection failed")
    return ColorDepth(
        DEFAULT_COLOR_DEPTH, "default", "nothing said what this terminal is"
    )


def apply_textual_color_system(
    depth: ColorDepth, environ: dict[str, str] | None = None
) -> str | None:
    """Pin Textual's colour system to *depth*, unless the user already has.

    ``textual.constants`` reads ``TEXTUAL_COLOR_SYSTEM`` once, at import,
    so this only does anything if it runs before Textual is imported --
    which is why it lives here and is called from ``__main__`` rather than
    from the app.  Returns the value now in force, or None if the variable
    could not be set at all.
    """
    env = os.environ if environ is None else environ
    system = TEXTUAL_COLOR_SYSTEMS.get(depth.value)
    if system is None:
        return env.get(TEXTUAL_ENV_VAR)
    return env.setdefault(TEXTUAL_ENV_VAR, system)
