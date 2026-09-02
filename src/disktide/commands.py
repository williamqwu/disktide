"""Command palette provider: every binding, findable by name.

This is the half of the keymap work that makes the rest cheap. With the
palette on, a binding no longer has to earn a key to be reachable, so the
footer is free to show six verbs instead of twenty-six, and the rare
destructive actions can drop their keys entirely rather than sitting on a
letter that means something harmless one screen over.

Commands come from the live bindings, so this cannot drift from the key map
or the footer — all three read the same `active_bindings`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Iterable

from textual.command import DiscoveryHit, Hit, Hits, Provider

from disktide.keys import ALL_IDS

if TYPE_CHECKING:
    from textual.dom import DOMNode


# Actions with no key at all. Archiving a monitor used to be `d` — the same
# finger as Explorer's harmless view toggle and Cleanup's delete plan — and
# it is rare and destructive, so it lost its key rather than moving to
# another one. The palette is now its only route, which is why it has to be
# named here: there is no binding left to walk.
#
# Keyed by screen class name so a command only offers itself where it works.
KEYLESS_ACTIONS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "MonitorScreen": (
        ("Archive monitor", "archive_monitor", "Stop and archive the selected monitor"),
    ),
    "ExplorerScreen": (
        ("Cell aspect: rounder", "nudge_cell_aspect(-1)", "Nudge the disc rounder"),
        ("Cell aspect: taller", "nudge_cell_aspect(1)", "Nudge the disc taller"),
    ),
}


def _runner(node: "DOMNode", action: str) -> Callable[[], object]:
    """Build the palette callback that fires one action on one node."""

    async def run() -> None:
        await node.app.run_action(action, default_namespace=node)

    return run


class BindingCommands(Provider):
    """Offers every binding on the active screen, plus the keyless actions."""

    def _commands(self) -> Iterable[tuple[str, str, Callable[[], object]]]:
        screen = self.screen
        seen: set[str] = set()

        for _key, active in screen.active_bindings.items():
            binding = active.binding
            binding_id = binding.id or ""
            # Our own bindings only. Everything the focused widget and
            # Textual contribute — cursor movement, scrolling, focus
            # stepping — is real but is not what someone opens a palette to
            # find, and it would bury the twenty commands that are.
            if binding_id not in ALL_IDS or binding_id in seen:
                continue
            seen.add(binding_id)

            title = binding.description or binding.action.replace("_", " ")
            key = self.app.get_key_display(binding)
            yield title, f"{binding_id}  ·  {key}", _runner(screen, binding.action)

        for title, action, help_text in KEYLESS_ACTIONS.get(
            type(screen).__name__, ()
        ):
            yield title, help_text, _runner(screen, action)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for title, help_text, run in self._commands():
            score = matcher.match(title)
            if score > 0:
                yield Hit(score, matcher.highlight(title), run, help=help_text)

    async def discover(self) -> Hits:
        for title, help_text, run in self._commands():
            yield DiscoveryHit(title, run, help=help_text)
