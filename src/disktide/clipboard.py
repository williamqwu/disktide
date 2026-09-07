"""Where a copied path actually goes, and which route can prove it landed.

`y` used to be one line -- ``App.copy_to_clipboard``, which writes
``\\x1b]52;c;<base64>\\a`` to the driver and nothing else. That is the right
sequence and it reaches almost nobody: the single most common way to run
this app on a login node is inside tmux, and **tmux drops an application's
OSC 52 unless ``set-clipboard`` is ``on``**. tmux 3.2a's ``input_osc_52``
opens with ``state = options_get_number(global_options, "set-clipboard");
if (state != 2) return;`` -- before it parses the payload, before it looks
at the target. The default is ``external`` (=1). Verified on a private
server here: under ``external`` a pane printing ``\\e]52;c;aGVsbG8=\\a``
created no buffer and forwarded nothing; under ``on`` it created
``buffer0``. So the key silently did nothing, and the toast that said
"Copied path" was making a claim the process had no way to check.

The fix is to stop pretending there is one clipboard and route to whatever
this environment actually has, verifiable routes first:

``tmux-buffer``
    ``tmux load-buffer -b disktide [-w] -``. The buffer is created whatever
    ``set-clipboard`` says, so this route *always* leaves the path
    somewhere the user can reach -- ``prefix ]`` pastes it in any pane, in
    any terminal, including a browser. ``-w`` ("also send the buffer to
    the clipboard using the xterm escape sequence") is tmux's *own* OSC 52
    write via ``tty_set_selection``, which honours ``set-clipboard
    on|external`` (only ``off`` blocks it) and needs the ``Ms`` capability
    -- which the default ``terminal-features`` gives every ``xterm*``
    client, OnDemand's ``xterm-16color`` included. ``-w`` and
    ``-t target-client`` arrived in tmux 3.2 (RHEL 8 ships 2.7, Ubuntu
    20.04 ships 3.0a), so below that the flag is dropped. Best of all this
    route has an **exit status**, which no escape sequence does.

``tmux-passthrough``
    ``\\ePtmux;`` + the sequence with every ``\\e`` doubled + ``\\e\\\\``,
    for tmux older than 3.2 where ``-w`` does not exist. tmux < 3.3 always
    allows passthrough (3.2a's ``input_dcs_dispatch`` has no checks);
    3.3+ needs ``allow-passthrough on``, which is why this is never used on
    a version that has ``-w``.

``screen-passthrough``
    GNU screen does not forward OSC 52 either. Its passthrough is ``\\eP``
    + chunk + ``\\e\\\\``, in chunks of at most 76 bytes.

``osc52``
    The bare sequence, for a terminal talking to us directly. It works in
    kitty, WezTerm, Alacritty, foot, iTerm2, Windows Terminal, VS Code,
    xterm with ``allowWindowOps``, and -- this is the one worth knowing --
    in **hterm**, which is what Open OnDemand's shell app actually is
    (``hterm_all_1.92.1.mod_1.js``; there is no xterm.js anywhere in it).
    hterm's ``hterm.VT.OSC['52']`` calls ``copyStringToClipboard`` →
    ``navigator.clipboard.writeText`` and pops its own "Selection Copied"
    overlay. It does *not* work in JupyterLab, whose xterm.js 6 is built
    without ``@xterm/addon-clipboard``, and not in macOS Terminal.app.

``pbcopy`` / ``wl-copy`` / ``xclip`` / ``xsel``
    A local clipboard tool, when a display is reachable. These are first
    when present: they have an exit status, and over ``ssh -X`` ``xclip``
    lands the text on the *local* X clipboard, which is exactly what the
    user meant. ``xclip`` and ``wl-copy`` fork and hold their pipes open
    until the selection is replaced, so they are run with stdout and
    stderr on ``DEVNULL`` and a timeout; without that ``subprocess.run``
    blocks until somebody else copies something.

Two consequences shape the API. First, **only some routes can be
confirmed** -- an escape sequence has no reply, ever -- so the result
carries which ones were verified by an exit status and the toast says
"unverifiable" rather than "copied" when none were. Second, this is pure
and injectable: ``environ``, ``which``, ``platform``, the tmux ``runner``
and the subprocess ``run`` all come in as arguments, because the four
environments this has to be right in (tmux 3.2a on a login node, tmux 2.7
on RHEL 8, a browser terminal, a laptop) cannot all be a developer's
shell. No Textual import, same as ``viz/colordepth``.

Nothing here raises. A clipboard that fails is an unconfirmed route and a
toast; it is never a traceback over the user's tree.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Callable

from disktide.viz.colordepth import TMUX_TIMEOUT, TmuxRunner, _run_tmux

__all__ = [
    "BUFFER_NAME",
    "ClipboardPlan",
    "ClipboardResult",
    "ClipboardRoute",
    "copy_text",
    "osc52",
    "parse_tmux_version",
    "plan_clipboard",
    "screen_passthrough",
    "tmux_passthrough",
]


#: The tmux buffer `y` writes to. Named rather than anonymous so pressing
#: `y` twenty times leaves one buffer instead of twenty `bufferN` entries,
#: and so the docs can tell a user what to paste.
BUFFER_NAME = "disktide"

#: Ceiling on a clipboard tool. `pbcopy` returns immediately; `xclip` and
#: `wl-copy` fork a selection owner and the parent exits, so a second is
#: generous for both and short enough not to be felt if one wedges.
TOOL_TIMEOUT = 1.0

#: tmux's own passthrough version boundary: `load-buffer -w` and
#: `-t target-client` are 3.2 features. Below this the DCS passthrough is
#: the only way to reach the outer terminal.
TMUX_WRITE_FLAG_VERSION = (3, 2)

#: GNU screen's DCS passthrough splits the payload; 76 bytes per chunk is
#: what every other tool that does this uses.
SCREEN_CHUNK = 76

# "tmux 3.2a", "tmux 2.7", "tmux next-3.4", "tmux 3.3a-openbsd". The
# letter suffix is a tmux release convention, not a patch number, so only
# the two leading integers are taken.
_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


def osc52(text: str, target: str = "c") -> str:
    """The OSC 52 sequence that asks a terminal to set its clipboard.

    ``\\x1b]52;<target>;<base64>\\x07`` -- byte-for-byte what
    ``App.copy_to_clipboard`` writes, so a terminal that honoured the old
    behaviour is not regressed by anything here. ``surrogateescape`` on
    the encode because a path that came off a filesystem can carry bytes
    that are not UTF-8, and refusing to copy such a path would be a worse
    answer than copying it back the way it arrived.
    """
    payload = base64.b64encode(text.encode("utf-8", "surrogateescape")).decode("ascii")
    return f"\x1b]52;{target};{payload}\x07"


def tmux_passthrough(seq: str) -> str:
    """Wrap *seq* so tmux hands it to the outer terminal untouched.

    ``\\ePtmux;`` … ``\\e\\\\`` with every ESC inside doubled, which is how
    tmux knows where the inner sequence's escapes are. Only used on tmux
    older than 3.2: from 3.2 ``load-buffer -w`` is tmux's own route and
    respects the user's ``set-clipboard``, and from 3.3 passthrough is
    off by default (``allow-passthrough``) and would silently do nothing.
    """
    return "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"


def screen_passthrough(seq: str, chunk: int = SCREEN_CHUNK) -> str:
    """Wrap *seq* for GNU screen, which forwards no OSC 52 of its own.

    screen's DCS passthrough is ``\\eP`` + payload + ``\\e\\\\`` and its
    input buffer is small, so the payload is split into ``chunk``-byte
    pieces and each piece gets its own wrapper. The pieces concatenate on
    the far side into exactly *seq*.
    """
    if chunk <= 0:
        chunk = SCREEN_CHUNK
    pieces = [seq[index:index + chunk] for index in range(0, len(seq), chunk)] or [""]
    return "".join(f"\x1bP{piece}\x1b\\" for piece in pieces)


def parse_tmux_version(text: str | None) -> tuple[int, int] | None:
    """``tmux -V`` output as ``(major, minor)``, or None if it did not say.

    None is the answer for every kind of "cannot tell" at once -- no
    output, a build that prints something else, ``tmux master`` -- because
    the caller treats them identically: assume the modern flag and let the
    retry sort it out, which is cheaper than refusing a route over a
    version string.
    """
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True, slots=True)
class ClipboardRoute:
    """One way to get the text to a clipboard, and whether it can say so.

    ``confirmable`` is the whole reason this is a dataclass rather than a
    string: a route with a ``command`` has an exit status and a route that
    writes an escape sequence does not, and the toast a user reads has to
    tell those apart. ``detail`` is the human sentence -- it is what the
    toast appends and what ``doctor`` prints, so it is written once here
    rather than reconstructed at each end.
    """

    name: str
    detail: str
    confirmable: bool
    command: tuple[str, ...] | None = None
    #: The escape sequence builder, for the routes that write rather than
    #: run. Takes the text, returns what goes to the driver.
    writes_sequence: Callable[[str], str] | None = None
    #: Flags to drop and try once more when the first attempt fails. Only
    #: ``-w`` uses it: a tmux that has the flag but cannot reach a client
    #: to write to should still leave the buffer behind.
    retry_without: tuple[str, ...] | None = None
    #: Extra environment for the command, e.g. the ``TMUX`` socket a test
    #: points at its own private server.
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClipboardPlan:
    """Every route this environment offers, in the order to try them.

    Built once per process: nothing in it changes while the app runs, and
    the tmux interrogation is a subprocess nobody wants to pay for twice.
    ``doctor`` prints this; the app executes it.
    """

    routes: tuple[ClipboardRoute, ...] = ()
    multiplexer: str | None = None
    tmux_version: tuple[int, int] | None = None
    #: What ``tmux -V`` actually said, minus the leading "tmux". The tuple
    #: above is what the policy compares; this is what a user recognises,
    #: because tmux's letter suffixes ("3.2a") are releases in their own
    #: right and dropping them makes a doctor line harder to match against
    #: a changelog than it needs to be.
    tmux_release: str | None = None
    tmux_set_clipboard: str | None = None
    tools: dict[str, bool] = field(default_factory=dict)
    suggestion: str | None = None

    @property
    def confirmable(self) -> bool:
        """Whether any route can report success rather than hope."""
        return any(route.confirmable for route in self.routes)


@dataclass(frozen=True, slots=True)
class ClipboardResult:
    """What actually happened, in the terms the toast has to speak.

    ``verified`` is not "the user can paste it" -- nothing can know that.
    It is "something returned exit status 0", which is the strongest claim
    available and the line between a toast that says *Copied path* and one
    that says the copy could not be confirmed.
    """

    plan: ClipboardPlan
    confirmed: tuple[str, ...] = ()
    attempted: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return bool(self.confirmed)

    @property
    def hint(self) -> str:
        """One line naming where the text went, for the toast.

        The first route that reported success, if any -- routes are
        planned verifiable-first, so that is also the one the user's next
        paste is most likely to read from. Failing that, the first one
        that was merely written, and failing that the honest answer.
        """
        for route in self.plan.routes:
            if route.name in self.confirmed:
                return route.detail
        for route in self.plan.routes:
            if route.name in self.attempted:
                return route.detail
        if self.failed:
            return "every clipboard route failed"
        return "no clipboard route was available"


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def _tool_route(
    which: Callable[[str], str | None], platform: str, environ: Mapping[str, str]
) -> tuple[ClipboardRoute | None, dict[str, bool]]:
    """The one local clipboard tool worth running here, if any.

    Presence is not enough: `xclip` on a login node with no `DISPLAY` sits
    there and fails, and `wl-copy` without `WAYLAND_DISPLAY` does the
    same. So each tool is gated on the variable that makes it meaningful,
    and only one route is produced -- the first that qualifies -- because
    a second write to the same clipboard cannot add anything.
    """
    tools = {
        name: bool(which(name))
        for name in ("pbcopy", "wl-copy", "xclip", "xsel")
    }
    if platform == "darwin" and tools["pbcopy"]:
        return (
            ClipboardRoute(
                name="pbcopy",
                detail="copied with pbcopy",
                confirmable=True,
                command=("pbcopy",),
            ),
            tools,
        )
    if environ.get("WAYLAND_DISPLAY") and tools["wl-copy"]:
        return (
            ClipboardRoute(
                name="wl-copy",
                detail="copied with wl-copy",
                confirmable=True,
                command=("wl-copy",),
            ),
            tools,
        )
    if environ.get("DISPLAY"):
        # Over `ssh -X` this is the *local* X clipboard, which is the
        # answer the user wanted and the one no escape sequence can give.
        if tools["xclip"]:
            return (
                ClipboardRoute(
                    name="xclip",
                    detail="copied with xclip (X clipboard selection)",
                    confirmable=True,
                    command=("xclip", "-selection", "clipboard", "-in"),
                ),
                tools,
            )
        if tools["xsel"]:
            return (
                ClipboardRoute(
                    name="xsel",
                    detail="copied with xsel (X clipboard selection)",
                    confirmable=True,
                    command=("xsel", "--clipboard", "--input"),
                ),
                tools,
            )
    return None, tools


def _tmux_buffer_route(
    version: tuple[int, int] | None, environ: Mapping[str, str]
) -> ClipboardRoute:
    """``load-buffer`` into the named buffer, with ``-w`` where it exists.

    ``-w`` is added when the version is unknown too: a tmux that cannot
    say what it is, is far more likely to be a modern one than a 2.x, and
    the cost of guessing wrong is one failed call and a retry that drops
    the flag -- against the cost of guessing the other way, which is a
    session that silently never reaches the terminal clipboard.
    """
    forward = version is None or version >= TMUX_WRITE_FLAG_VERSION
    command: tuple[str, ...] = ("tmux", "load-buffer", "-b", BUFFER_NAME)
    if forward:
        command += ("-w",)
    command += ("-",)
    detail = f"tmux buffer {BUFFER_NAME} · paste with prefix ]"
    if forward:
        detail += ", and forwarded to the terminal clipboard"
    return ClipboardRoute(
        name="tmux-buffer",
        detail=detail,
        confirmable=True,
        command=command,
        retry_without=("-w",) if forward else None,
        env={"TMUX": environ["TMUX"]} if environ.get("TMUX") else {},
    )


def _suggestion(
    *,
    set_clipboard: str | None,
    version: tuple[int, int] | None,
    in_tmux: bool,
    tool: ClipboardRoute | None,
    only_osc52: bool,
) -> str | None:
    """What to change, printed only where there is something to gain.

    Same discipline as ``doctor``'s colour suggestion: silence is the
    default, and a line appears only when the environment is costing the
    user something a single setting would give back.
    """
    if in_tmux and set_clipboard == "off":
        return (
            "tmux is configured not to touch the terminal clipboard "
            "(set -g set-clipboard off); `y` still fills tmux buffer "
            f"{BUFFER_NAME} — paste with prefix ], or set it to external"
        )
    if in_tmux and version is not None and version < TMUX_WRITE_FLAG_VERSION:
        return (
            f"tmux {version[0]}.{version[1]} cannot forward a buffer to the "
            "terminal (load-buffer -w needs 3.2); `y` uses DCS passthrough "
            f"instead and fills tmux buffer {BUFFER_NAME}"
        )
    if only_osc52 and tool is None:
        return (
            "nothing here can confirm a copy: `y` writes OSC 52, which "
            "kitty, WezTerm, Alacritty, foot, iTerm2, Windows Terminal, "
            "VS Code and Open OnDemand's hterm honour, but macOS "
            "Terminal.app and JupyterLab's xterm.js drop. Press Y to show "
            "the path for hand selection"
        )
    return None


def plan_clipboard(
    environ: Mapping[str, str] | None = None,
    *,
    runner: TmuxRunner | None = None,
    which: Callable[[str], str | None] = shutil.which,
    platform: str = sys.platform,
) -> ClipboardPlan:
    """Every route worth trying here, verifiable ones first.

    The order is a claim about evidence, not about quality. A local tool
    and a tmux buffer both return an exit status, so they come before any
    escape sequence, which returns nothing ever. Inside tmux there is
    deliberately **no raw ``osc52`` route**: under the default
    ``set-clipboard external`` tmux drops it before parsing (the bug this
    module exists for), and under ``on`` it would merely duplicate what
    ``load-buffer -w`` already sent.

    Every probe is optional. A tmux that cannot be asked, a ``which`` that
    finds nothing, an environment with no ``DISPLAY`` -- each costs one
    route and nothing else.
    """
    env = os.environ if environ is None else environ
    run = _run_tmux if runner is None else runner

    routes: list[ClipboardRoute] = []
    tool, tools = _tool_route(which, platform, env)
    if tool is not None:
        routes.append(tool)

    multiplexer: str | None = None
    version: tuple[int, int] | None = None
    release: str | None = None
    set_clipboard: str | None = None

    if env.get("TMUX"):
        multiplexer = "tmux"
        raw_version = run(["-V"])
        version = parse_tmux_version(raw_version)
        if version is not None:
            release = (raw_version or "").strip().removeprefix("tmux").strip() or None
        reachable = version is not None
        if reachable:
            raw = run(["show", "-gv", "set-clipboard"])
            set_clipboard = (raw or "").strip() or None
            routes.append(_tmux_buffer_route(version, env))
            if version < TMUX_WRITE_FLAG_VERSION:
                routes.append(
                    ClipboardRoute(
                        name="tmux-passthrough",
                        detail=(
                            "OSC 52 passed through tmux to the terminal "
                            "(no reply to read)"
                        ),
                        confirmable=False,
                        writes_sequence=lambda text: tmux_passthrough(osc52(text)),
                    )
                )
        else:
            # TMUX is set but the server cannot be asked: a wedged one, a
            # `tmux` that is not on this PATH, a socket from a dead
            # session. There is no buffer to write into, so fall back to
            # the sequence and let tmux pass it through if it will.
            routes.append(
                ClipboardRoute(
                    name="osc52",
                    detail="OSC 52 written to the terminal (no reply to read)",
                    confirmable=False,
                    writes_sequence=osc52,
                )
            )
            routes.append(
                ClipboardRoute(
                    name="tmux-passthrough",
                    detail=(
                        "OSC 52 passed through tmux to the terminal "
                        "(no reply to read)"
                    ),
                    confirmable=False,
                    writes_sequence=lambda text: tmux_passthrough(osc52(text)),
                )
            )
    elif env.get("STY"):
        multiplexer = "screen"
        routes.append(
            ClipboardRoute(
                name="screen-passthrough",
                detail=(
                    "OSC 52 passed through screen to the terminal "
                    "(no reply to read)"
                ),
                confirmable=False,
                writes_sequence=lambda text: screen_passthrough(osc52(text)),
            )
        )
    else:
        routes.append(
            ClipboardRoute(
                name="osc52",
                detail="OSC 52 written to the terminal (no reply to read)",
                confirmable=False,
                writes_sequence=osc52,
            )
        )

    only_osc52 = [route.name for route in routes] == ["osc52"]
    return ClipboardPlan(
        routes=tuple(routes),
        multiplexer=multiplexer,
        tmux_version=version,
        tmux_release=release,
        tmux_set_clipboard=set_clipboard,
        tools=tools,
        suggestion=_suggestion(
            set_clipboard=set_clipboard,
            version=version,
            in_tmux=multiplexer == "tmux",
            tool=tool,
            only_osc52=only_osc52,
        ),
    )


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


CommandRunner = Callable[
    [tuple[str, ...], bytes, float, "Mapping[str, str] | None"], "int | None"
]


def _run_command(
    command: tuple[str, ...],
    payload: bytes,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> int | None:
    """Feed *payload* to *command* on stdin; return its exit status or None.

    ``stdout`` and ``stderr`` go to ``DEVNULL`` and not to a pipe, and
    that is load-bearing rather than tidy: ``xclip`` and ``wl-copy`` fork
    a selection owner that inherits the pipes and holds them open until
    somebody else copies something, so ``capture_output=True`` here does
    not return when the command exits -- it returns when the user's next
    copy happens, which may be never. The timeout is the second belt.

    None is every kind of "could not run it": no such binary, a timeout, a
    command line the OS refused. The caller records those as failures, the
    same as a non-zero status, because from the user's side they are.
    """
    try:
        completed = subprocess.run(
            list(command),
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=None if env is None else dict(env),
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return completed.returncode


def copy_text(
    text: str,
    plan: ClipboardPlan,
    *,
    write: Callable[[str], None],
    run: CommandRunner | None = None,
) -> ClipboardResult:
    """Take every route in *plan*, and report which ones could prove it.

    Every route is taken, not just the first that works. They land in
    different places -- an X clipboard, a tmux buffer, the outer
    terminal's clipboard -- and a user who pressed one key should not have
    to know which of them their next paste will read from.

    Nothing raises. A route that fails is recorded and the next one runs;
    a plan where all of them fail still returns a result, and the toast
    tells the truth about it. Losing a copy is an annoyance; losing the
    session over a missing binary is not.
    """
    execute = _run_command if run is None else run
    environ = os.environ

    confirmed: list[str] = []
    attempted: list[str] = []
    failed: list[str] = []

    for route in plan.routes:
        if route.command is not None:
            command = route.command
            timeout = TMUX_TIMEOUT if command[0] == "tmux" else TOOL_TIMEOUT
            env = {**environ, **route.env} if route.env else None
            payload = text.encode("utf-8", "surrogateescape")
            status = execute(command, payload, timeout, env)
            if status != 0 and route.retry_without:
                # A tmux new enough for `-w` can still fail it -- no
                # client attached to write to, `Ms` missing on the one
                # that is. The buffer is the part worth keeping, so drop
                # the flag and try once more rather than losing both.
                retry = tuple(
                    part for part in command if part not in route.retry_without
                )
                if retry != command:
                    status = execute(retry, payload, timeout, env)
            if status == 0:
                confirmed.append(route.name)
            else:
                failed.append(route.name)
            continue
        if route.writes_sequence is not None:
            try:
                write(route.writes_sequence(text))
            except Exception:
                failed.append(route.name)
                continue
            attempted.append(route.name)

    return ClipboardResult(
        plan=plan,
        confirmed=tuple(confirmed),
        attempted=tuple(attempted),
        failed=tuple(failed),
    )
