"""Wait on a running app without swallowing the reason the wait failed.

A bounded ``for ... break`` that falls through in silence leaves the test
driving a screen that never finished booting, and the test then dies
wherever it first touches that screen: ``NoMatches: '#size-tree'`` out of
``ExplorerScreen._apply_tree_snapshot``, or ``'NoneType' object has no
attribute 'children'`` out of ``build_live_view``.  Both name a widget or
an attribute.  Neither names the scan that never finished, which is the
only fact that would have explained the run -- and under ``-n 16`` the
traceback is all anyone gets.

The margin is why this only ever shows up on a busy machine: an unloaded
host boots the explorer in two or three pumps against a budget of thirty,
so the fall-through is unreachable until sixteen of these run at once on
a fifteen-core box.  That is exactly when a legible message is worth the
most, and exactly when nobody can reproduce it to go looking.

Budgets are counted in message-loop pumps rather than wall-clock seconds
on purpose.  A ``pause`` that has to wait its turn on a loaded host takes
longer than the delay it was given, so a pump count stretches with the
contention that caused the wait, where a deadline would expire in the
middle of it.

Every wait here ends in an assertion.
"""

from __future__ import annotations

import inspect

from disktide.screens.explorer import ExplorerScreen

# One boot's worth of pumps.  The explorer needs two or three of them
# unloaded, and more than thirty on a fifteen-core box running the suite
# under `-n 16`.  A budget is only ever spent in full by a test that is
# going to fail, so it is set for the worst host rather than the median
# one: the cost of the headroom is zero every time the wait succeeds.
EXPLORER_TRIES = 100
EXPLORER_DELAY = 0.1


def describe(predicate) -> str:
    """Name a predicate by its own source so callers need not repeat it.

    The lambdas these waits take are written inline at the call site, so
    ``getsource`` returns the whole ``await wait_until(...)`` statement --
    which is a better description of the condition than anything worth
    hand-writing 20 times.
    """
    try:
        source = " ".join(inspect.getsource(predicate).split())
    except (OSError, TypeError):  # pragma: no cover - defensive
        return repr(predicate)
    return source[:200]


async def wait_until(
    pilot,
    predicate,
    *,
    what: str | None = None,
    tries: int = 100,
    delay: float = 0.05,
) -> None:
    """Pump until ``predicate`` holds, or say which condition never did."""
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause(delay)
    if predicate():
        return
    raise AssertionError(
        f"condition never held after {tries} pumps "
        f"({tries * delay:.1f}s of pauses): {what or describe(predicate)}"
    )


async def wait_for_explorer(
    pilot,
    app,
    *,
    tries: int = EXPLORER_TRIES,
    delay: float = EXPLORER_DELAY,
) -> ExplorerScreen:
    """Return the explorer once it holds a finished scan and a live tree.

    The size-tree is part of the condition, not an afterthought: it is the
    widget every caller reaches for next, and a screen that is current but
    not yet composed is the state that produced ``NoMatches: '#size-tree'``
    from deep inside production code.
    """

    def ready() -> ExplorerScreen | None:
        screen = app.screen
        if (
            isinstance(screen, ExplorerScreen)
            and screen._root is not None
            and screen.is_mounted
            and screen.query("#size-tree")
        ):
            return screen
        return None

    await pilot.pause(delay * 2)
    for _ in range(tries):
        screen = ready()
        if screen is not None:
            return screen
        await pilot.pause(delay)
    # One last look, so the state named below is the state that failed and
    # not one that arrived while the message was being built.
    screen = ready()
    if screen is not None:
        return screen

    current = app.screen
    if isinstance(current, ExplorerScreen):
        composed = current.is_mounted and current.query("#size-tree")
        tree = "present" if composed else "absent"
        state = (
            f"root={'set' if current._root is not None else 'None'}, "
            f"mounted={current.is_mounted}, size-tree={tree}"
        )
    else:
        state = f"still on {type(current).__name__}"
    raise AssertionError(
        f"the explorer never finished booting after {tries} pumps "
        f"({tries * delay:.1f}s of pauses): {state}"
    )


async def wait_for_layout(pilot, view, *, tries: int = 60, delay: float = 0.05) -> None:
    """Wait out a chart's build, including any rebuild it has already queued.

    A layout that is present and not `_stale` can still be one paint away
    from being replaced: an epoch or size change noticed mid-paint is
    deferred to `call_next` rather than swapped in under a half-drawn
    frame (see `SunburstView._invalidate_after_paint`).  A caller that
    stops at `not _stale` captures the layout that pending rebuild is
    about to discard, and only finds out several pumps later.
    """
    await wait_until(
        pilot,
        lambda: (
            view._layout is not None
            and not view._stale
            and not getattr(view, "_invalidate_scheduled", False)
        ),
        what=f"{type(view).__name__} never built a settled layout",
        tries=tries,
        delay=delay,
    )
