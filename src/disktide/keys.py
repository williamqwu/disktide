"""Binding identity, footer grouping, and the named key presets.

Three things live here, and they exist because the footer, the key map and
``config.toml`` all need to agree about the same 93 bindings.

**Ids are a public contract.** Every ``Binding`` in the app carries an ``id=``
from :data:`ALL_IDS`. Those ids are what a user writes into the ``[keys]``
table of ``config.toml`` and what :meth:`textual.app.App.set_keymap` looks up.
Renaming one silently breaks that user's config, so treat a rename the way you
would treat a CLI flag rename: don't, or add the old name to
:data:`_ID_ALIASES`.

**Groups are what make the footer fit.** Textual's ``Footer`` collapses a run
of bindings that share a ``group`` into their bare keys plus one shared label,
instead of repeating a description per key. That is the mechanism the old
hand-packed descriptions (``"[U]p [I]nto"``, ``key_display="^D/^U"``) were
imitating. Two constraints come with it: the group objects must compare equal
(hence module-level constants, not inline construction), and ``Footer`` uses
``itertools.groupby``, so **bindings in the same group must be declared
adjacently** or the group splits into two.

**Presets are deltas, not full maps.** The keys declared in the source *are*
the ``spine`` layout. A preset lists only the ids whose key differs from that,
so ``PRESETS["spine"]`` is empty by construction and the other two describe
themselves in terms of what they move back.
"""

from __future__ import annotations

from textual.binding import Binding

__all__ = [
    "ACTION",
    "ALERT",
    "ALL_IDS",
    "DEFAULT_PRESET",
    "DIFF",
    "MODE",
    "NAV",
    "PLAN",
    "PRESETS",
    "SELECT",
    "SNAPSHOT",
    "TREND",
    "VIEW",
    "SECTIONS",
    "SECTION_ORDER",
    "canonical_id",
    "section_for",
    "resolve_keymap",
]


# --------------------------------------------------------------------------
# Footer groups
#
# `compact=True` drops the inter-key spacing, which is what you want for a run
# of single-character keys that read as one control (mode digits, navigation).
# Leave it False where the keys are wide enough to need the air.
# --------------------------------------------------------------------------

MODE = Binding.Group("Mode", compact=True)
NAV = Binding.Group("Nav", compact=True)
VIEW = Binding.Group("View", compact=True)
ACTION = Binding.Group("Action", compact=True)
PLAN = Binding.Group("Plan", compact=True)
SELECT = Binding.Group("Select", compact=True)
ALERT = Binding.Group("Alert", compact=True)
SNAPSHOT = Binding.Group("Snapshot", compact=True)
TREND = Binding.Group("Trend", compact=True)
DIFF = Binding.Group("Diff", compact=True)


# --------------------------------------------------------------------------
# The id vocabulary
#
# Grouped by the screen that declares them, in declaration order, so this
# reads as a table of contents for the key map. Adding a binding means adding
# its id here; `tests/test_keymap.py` asserts the two stay in step.
# --------------------------------------------------------------------------

APP_IDS = (
    "app.mode_explorer",
    "app.mode_monitor",
    "app.mode_fs_overview",
    "app.mode_cleanup",
    "app.keymap",
    "app.settings",
    "app.quit",
)

EXPLORER_IDS = (
    "explorer.viz_sunburst",
    "explorer.viz_treemap",
    "explorer.viz_details",
    "explorer.up",
    "explorer.into",
    "explorer.sort",
    "explorer.rescan",
    "explorer.diff",
    "explorer.snapshot_newer",
    "explorer.snapshot_older",
    "explorer.ring_shape",
    "explorer.setup_monitor",
    "explorer.yank",
    "explorer.show_path",
    "explorer.metric",
    "explorer.scroll_down",
    "explorer.scroll_up",
)

CLEANUP_IDS = (
    "cleanup.review_plan",
    "cleanup.select_all",
    "cleanup.toggle",
    "cleanup.refresh",
    "cleanup.undo",
    "cleanup.history",
    "cleanup.map",
)

MONITOR_IDS = (
    "monitor.new",
    "monitor.edit",
    "monitor.pause",
    "monitor.run_now",
    "monitor.reconcile",
    "monitor.sampling",
    "monitor.pin",
    "monitor.alert_add",
    "monitor.alert_edit",
    "monitor.alert_toggle",
    "monitor.alert_remove",
    "monitor.retention",
    "monitor.refresh",
    "monitor.detail",
    "monitor.back",
    "monitor.viz_trend",
    "monitor.viz_treemap",
    "monitor.viz_sunburst",
    "monitor.viz_heatmap",
    "monitor.viz_next",
    "monitor.trend_zoom",
    "monitor.trend_older",
    "monitor.trend_newer",
    "monitor.baseline",
    "monitor.target",
    "monitor.latest_pair",
)

FS_IDS = (
    "fs.refresh",
    "fs.benchmark",
)

MODAL_IDS = (
    "alert.cancel",
    "alert.field_next",
    "alert.field_previous",
    "alert.save",
    "confirm.cancel",
    "confirm.no",
    "confirm.yes",
    "editor.cancel",
    "editor.field_next",
    "editor.field_previous",
    "editor.save",
    "fs.detail_close",
    "fs.detail_close_enter",
    "fs.detail_close_q",
    "fs.device_close",
    "fs.device_close_enter",
    "fs.device_close_q",
    "heatmap.down",
    "heatmap.select",
    "heatmap.up",
    "keymap.close_q",
    "map.next",
    "map.next_down",
    "map.previous",
    "map.previous_up",
    "map.select",
    "modal.path_close",
    "modal.path_close_enter",
    "modal.path_close_q",
    "settings.back",
    "settings.field_next",
    "settings.field_previous",
    "welcome.quit",
    "keymap.close",
)

ALL_IDS: frozenset[str] = frozenset(
    APP_IDS + EXPLORER_IDS + CLEANUP_IDS + MONITOR_IDS + FS_IDS + MODAL_IDS
)


# --------------------------------------------------------------------------
# Presets
#
# The declared keys are `spine`. A preset maps id -> key for the ids it moves;
# everything absent keeps the declared key. `classic` reproduces the layout
# shipped up to v0.2.30, so an existing user can put one line in config.toml
# and get their fingers back.
#
# `monitor.archive` is deliberately absent from every preset: archiving a
# monitor is rare and destructive, and it now lives in the command palette
# only. There is no key to move.
# --------------------------------------------------------------------------

_SAFE_DELTAS: dict[str, str] = {
    # `classic` bound Cleanup's plan review and undo to `d` and `u`, the same
    # fingers Explorer uses for a harmless view toggle and a directory step,
    # and Monitor's `s` started a background sampling host where Explorer's
    # `s` only cycles a sort. Those three are the safety fixes; `safe` takes
    # them and nothing else.
    "app.mode_cleanup": "c",
    "app.keymap": "ctrl+k",
    "app.settings": "question_mark",
    "fs.benchmark": "b",
}

_CLASSIC_DELTAS: dict[str, str] = {
    **_SAFE_DELTAS,
    "cleanup.review_plan": "d",
    "cleanup.undo": "u",
    "monitor.sampling": "s",
}

PRESETS: dict[str, dict[str, str]] = {
    # Nothing to override: the source already declares this layout.
    "spine": {},
    "safe": _SAFE_DELTAS,
    "classic": _CLASSIC_DELTAS,
}

DEFAULT_PRESET = "spine"

# Ids that have been renamed. Maps the retired name to the current one so an
# older config.toml keeps working. Empty today; add rather than rename.
_ID_ALIASES: dict[str, str] = {}


# --------------------------------------------------------------------------
# Key map sections
#
# The footer can only ever carry a handful of verbs, so most bindings live in
# `?`. A flat list of sixteen rows hides a key just as effectively as no list
# at all -- `g` (ring shape) was in the key map the whole time and still read
# as missing -- so each screen's rows are broken into sections, and the niche
# keys sit next to the everyday ones they belong with rather than at the
# bottom of an undifferentiated column.
#
# This is deliberately *not* `Binding.group`. That one is a footer concern:
# for a visible binding it collapses the keys under a single shared label,
# which is right for `u`/`i` and wrong for a section heading. Keeping the two
# separate means a section can be renamed without moving anything in the
# footer.
# --------------------------------------------------------------------------

MOVE = "Move around"
VIEW_SECTION = "Choose the view"
COMPARE = "Compare snapshots"
SELECTION = "Selection"
THE_PLAN = "The plan"
MONITORS = "Monitors"
ALERTS = "Alerts"
OTHER = "Other actions"

# Order sections appear in, everywhere. A screen only shows the ones it has.
SECTION_ORDER: tuple[str, ...] = (
    MOVE,
    SELECTION,
    THE_PLAN,
    MONITORS,
    VIEW_SECTION,
    COMPARE,
    ALERTS,
    OTHER,
)

SECTIONS: dict[str, str] = {
    # Explorer
    "explorer.up": MOVE,
    "explorer.into": MOVE,
    "explorer.scroll_down": MOVE,
    "explorer.scroll_up": MOVE,
    "explorer.viz_sunburst": VIEW_SECTION,
    "explorer.viz_treemap": VIEW_SECTION,
    "explorer.viz_details": VIEW_SECTION,
    "explorer.ring_shape": VIEW_SECTION,
    "explorer.sort": VIEW_SECTION,
    "explorer.metric": VIEW_SECTION,
    "explorer.diff": COMPARE,
    "explorer.snapshot_newer": COMPARE,
    "explorer.snapshot_older": COMPARE,
    "explorer.yank": OTHER,
    "explorer.show_path": OTHER,
    "explorer.rescan": OTHER,
    "explorer.setup_monitor": OTHER,
    # Cleanup
    "cleanup.toggle": SELECTION,
    "cleanup.select_all": SELECTION,
    "cleanup.review_plan": THE_PLAN,
    "cleanup.undo": THE_PLAN,
    "cleanup.history": OTHER,
    "cleanup.map": OTHER,
    "cleanup.refresh": OTHER,
    # Monitor
    "monitor.detail": MOVE,
    "monitor.back": MOVE,
    "monitor.new": MONITORS,
    "monitor.edit": MONITORS,
    "monitor.pause": MONITORS,
    "monitor.run_now": MONITORS,
    "monitor.sampling": MONITORS,
    "monitor.reconcile": MONITORS,
    "monitor.refresh": MONITORS,
    "monitor.viz_trend": VIEW_SECTION,
    "monitor.viz_treemap": VIEW_SECTION,
    "monitor.viz_sunburst": VIEW_SECTION,
    "monitor.viz_heatmap": VIEW_SECTION,
    "monitor.viz_next": VIEW_SECTION,
    "monitor.trend_zoom": VIEW_SECTION,
    "monitor.trend_older": VIEW_SECTION,
    "monitor.trend_newer": VIEW_SECTION,
    "monitor.baseline": COMPARE,
    "monitor.target": COMPARE,
    "monitor.latest_pair": COMPARE,
    "monitor.pin": COMPARE,
    "monitor.alert_add": ALERTS,
    "monitor.alert_edit": ALERTS,
    "monitor.alert_toggle": ALERTS,
    "monitor.alert_remove": ALERTS,
    "monitor.retention": OTHER,
    # FS Overview
    "fs.benchmark": OTHER,
    "fs.refresh": OTHER,
}


def section_for(binding_id: str) -> str:
    """Section heading a binding belongs under in the key map."""
    return SECTIONS.get(binding_id, OTHER)


def canonical_id(binding_id: str) -> str:
    """Resolve a possibly-retired binding id to its current name."""
    return _ID_ALIASES.get(binding_id, binding_id)


def resolve_keymap(
    preset: str | None = None,
    overrides: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Build the ``id -> key`` map to hand to :meth:`App.set_keymap`.

    A user's own ``[keys]`` entries are applied on top of the preset, so
    ``preset = "classic"`` plus one override is a coherent thing to write.

    Returns the keymap and a list of human-readable problems (an unknown
    preset name, an id that no longer exists). Problems are returned rather
    than raised: a typo in ``config.toml`` should cost the user that one
    binding, not the whole app.
    """
    problems: list[str] = []

    name = (preset or DEFAULT_PRESET).strip().lower()
    if name not in PRESETS:
        problems.append(
            f"unknown key preset {name!r}; using {DEFAULT_PRESET!r}. "
            f"Valid presets: {', '.join(sorted(PRESETS))}."
        )
        name = DEFAULT_PRESET

    keymap = dict(PRESETS[name])

    for raw_id, key in (overrides or {}).items():
        binding_id = canonical_id(str(raw_id))
        if binding_id not in ALL_IDS:
            problems.append(f"[keys] {raw_id!r} is not a known binding id; ignored.")
            continue
        if not isinstance(key, str) or not key.strip():
            problems.append(f"[keys] {raw_id!r} has an empty key; ignored.")
            continue
        keymap[binding_id] = key.strip()

    return keymap, problems
