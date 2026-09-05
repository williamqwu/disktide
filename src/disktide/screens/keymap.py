"""The key map: every binding live on the screen underneath, grouped.

This is what `?` opens. It exists because the footer can only ever show a
handful of verbs, and before this screen the only complete key reference was
`docs/user-guide.md` — which documented 1 of Monitor's 26 bindings.

It reads the bindings rather than restating them, so it cannot drift: the
source of truth is whatever `active_bindings` reports for the screen this map
describes, after `[keys]` overrides have been applied.

Two things here are less obvious than they look, and both were bugs first.

**The screen being described is passed in, not looked up.** `compose` runs
before `push_screen` appends this screen to the stack in a real terminal, and
after it under `run_test` — so `screen_stack[-2]` meant "the screen below"
in the test harness and "the app's default screen" in front of a user, which
is why the map listed nothing but the app-level bindings in a real terminal
while every test passed. See :meth:`KeymapScreen._under`.

**The rows pack into columns.** Twenty-odd one-per-line rows plus headings
overflow an 80x24 terminal, and a key you have to scroll to reach is a key
you do not know about — the failure this screen exists to fix. Each section
takes as many columns as its widest row allows, which puts Explorer's map at
twenty rows instead of thirty-five.
"""

from __future__ import annotations

from math import ceil

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.geometry import Size
from textual.screen import ModalScreen, Screen
from textual.widgets import Static

from disktide.keys import ALL_IDS, DEFAULT_PRESET, SECTION_ORDER, section_for

# Ids under these prefixes are the spine: same key, same meaning, every
# screen. They are listed first and separately, because "what is always
# true" is the half a user needs to memorise.
_SPINE_PREFIX = "app."

# Bindings that are structural rather than actions — modal dismissal, field
# stepping, and the arrow keys a focused widget uses to move its own cursor.
# Listing them would triple the map without teaching anything: the Age/Size
# map alone contributes four rows that say "previous candidate" and "next
# candidate" twice each, once per arrow.
_HIDDEN_PREFIXES = ("modal.", "keymap.", "map.", "heatmap.", "confirm.",
                    "alert.", "editor.", "settings.", "welcome.", "fs.detail",
                    "fs.device")

# Class names do not survive contact with a title bar: FSOverviewScreen is
# "FS Overview", not "FSOverview".
_SCREEN_NAMES = {
    "ExplorerScreen": "Explorer",
    "MonitorScreen": "Monitor",
    "FSOverviewScreen": "FS Overview",
    "CleanupScreen": "Cleanup",
    "SettingsScreen": "Settings",
}

# The heading the spine sits under. Not in `SECTION_ORDER`: it is the same on
# every screen and always comes first.
SPINE_HEADING = "Always available"

# Space between packed columns. Two would read as one wide column on the
# sections whose descriptions are short ("Sort", "Bar").
COLUMN_GAP = 3

# What to pack against before the widget has a width of its own: the
# dialog's 76 columns less its border, its padding, the rows' own indent and
# a scrollbar.
_DEFAULT_WIDTH = 67


def column_count(
    rows: list[tuple[str, str]],
    key_width: int,
    width: int,
    gap: int = COLUMN_GAP,
) -> int:
    """How many columns of `rows` fit in `width`, at least one."""
    if not rows:
        return 1
    cell = _cell_width(rows, key_width)
    # One column always, even if the dialog is narrower than a single cell:
    # a wrapped line beats a dropped one.
    return max(1, min(len(rows), (width + gap) // (cell + gap)))


def _cell_width(rows: list[tuple[str, str]], key_width: int) -> int:
    return key_width + 2 + max(len(description) for _key, description in rows)


def pack_rows(
    rows: list[tuple[str, str]],
    key_width: int,
    width: int,
    gap: int = COLUMN_GAP,
) -> Text:
    """Lay one section's rows out in as many columns as `width` allows.

    Every cell is the same width — the section's widest description plus the
    map-wide key column — so the keys line up down each column and across
    sections. Filling is row-major, which keeps reading order left-to-right.
    """
    if not rows:
        return Text()

    cell = _cell_width(rows, key_width)
    columns = column_count(rows, key_width, width, gap)

    text = Text()
    for index, (key, description) in enumerate(rows):
        if index and index % columns == 0:
            text.append("\n")
        text.append(key.rjust(key_width), style="bold")
        text.append("  ")
        # Padding the last cell on a line would be invisible trailing space,
        # and it makes the rendered text harder to assert on.
        if index % columns == columns - 1 or index == len(rows) - 1:
            text.append(description)
        else:
            text.append(description.ljust(cell - key_width - 2))
            text.append(" " * gap)
    return text


class _Rows(Static):
    """A section's rows, packed to whatever width the dialog ends up with.

    The packing happens during layout rather than in an `on_resize` handler.
    A handler was the obvious way to write this and it was subtly wrong: a
    container caches its arrangement by `(size, child count)`, so a child
    that changes its own height *after* the resize that prompted it leaves
    the container laid out for the old height. That is a stray blank line
    under whichever section the terminal's width happened to re-pack.
    """

    def __init__(self, rows: list[tuple[str, str]], key_width: int) -> None:
        super().__init__(classes="keymap-rows")
        self._rows = rows
        self._key_width = key_width

    def render(self) -> Text:
        width = self.content_size.width or _DEFAULT_WIDTH
        return pack_rows(self._rows, self._key_width, width)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        if not self._rows:
            return 0
        columns = column_count(self._rows, self._key_width, width)
        return ceil(len(self._rows) / columns)


class KeymapScreen(ModalScreen[None]):
    """A full, grouped key reference for the screen it was opened from."""

    DEFAULT_CSS = """
    KeymapScreen {
        align: center middle;
        background: $background 60%;
    }

    /* The title and the way out ride in the border, which costs no rows
       and cannot scroll away. Docking two Statics instead looked identical
       and was wrong: Textual counts a docked child in the scrollable
       height, so the last section became unreachable by exactly the height
       of the chrome. */
    #keymap-dialog {
        width: 76;
        max-width: 96%;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: double $primary;
        border-title-style: bold;
        border-title-color: $foreground;
        border-subtitle-color: $text-muted;
        padding: 1 2;
    }

    .keymap-section {
        text-style: bold;
        color: $accent;
        padding-top: 1;
    }

    .keymap-section.-first {
        padding-top: 0;
    }

    .keymap-rows {
        padding-left: 1;
    }

    #keymap-hint {
        color: $text-muted;
        padding-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=True, id="keymap.close"),
        Binding("q", "dismiss", "Close", show=False, id="keymap.close_q"),
    ]

    def __init__(self, describes: Screen | None = None) -> None:
        """`describes` is the screen whose keys to list — see `_under`."""
        super().__init__()
        self._describes = describes

    def compose(self) -> ComposeResult:
        # One scroll region, not a scrolling body inside a scrolling dialog:
        # the inner one carried a fixed `max-height` that ignored the
        # terminal, so on anything shorter than 44 rows its last section was
        # laid out past the bottom of the dialog and could not be scrolled to.
        dialog = VerticalScroll(id="keymap-dialog")
        dialog.border_title = self._title()
        dialog.border_subtitle = "esc closes"
        with dialog:
            spine, sections = self._collect()
            blocks = ([(SPINE_HEADING, spine)] if spine else []) + sections
            for index, (heading, rows) in enumerate(blocks):
                # Key column per section, not one for the whole map: a
                # section is the visual unit now that rows pack sideways,
                # and one wide key elsewhere on the screen should not indent
                # every other section past it. Monitor's `shift+←` was
                # pushing the mode digits nine columns in.
                key_width = max((len(key) for key, _ in rows), default=0)
                classes = "keymap-section" if index else "keymap-section -first"
                yield Static(heading, classes=classes)
                yield _Rows(rows, key_width)
            yield Static(Text(self._hint()), id="keymap-hint")

    # -- data ---------------------------------------------------------------

    def _under(self) -> Screen | None:
        """The screen this map is describing.

        Whoever opened the map passes it in, because the stack cannot be
        trusted at this point: `push_screen` mounts the screen — and so runs
        `compose` — before appending it, and whether the append wins the race
        differs between a real terminal and `run_test`. The fallback is for a
        direct `push_screen(KeymapScreen())`: take the top screen that is not
        this one, which is right in either order.
        """
        if self._describes is not None:
            return self._describes
        for screen in reversed(self.app.screen_stack):
            if screen is not self:
                return screen
        return None

    def _mode_name(self) -> str:
        under = self._under()
        if under is None:
            return "This screen"
        name = type(under).__name__
        return _SCREEN_NAMES.get(name) or name.removesuffix("Screen") or "This screen"

    def _collect(
        self,
    ) -> tuple[
        list[tuple[str, str]],
        list[tuple[str, list[tuple[str, str]]]],
    ]:
        """Split the live bindings into the spine and this screen's sections.

        Returns `(spine_rows, [(heading, rows), ...])`. Sections come back in
        `SECTION_ORDER` and empty ones are dropped, so a screen shows only the
        headings it actually has.
        """
        under = self._under()
        if under is None:
            return [], []

        spine: list[tuple[str, str]] = []
        by_section: dict[str, list[tuple[str, str]]] = {}
        seen: set[str] = set()

        for _key, active in under.active_bindings.items():
            binding = active.binding
            binding_id = binding.id or ""
            # Only this app's own bindings. `active_bindings` also carries
            # everything the focused widget and Textual itself contribute —
            # cursor movement, scrolling, focus stepping, `ctrl+q` — which
            # would bury the twenty rows a user is actually looking for. Our
            # ids are the filter, which is the second job they do after
            # `[keys]`.
            if binding_id not in ALL_IDS:
                continue
            if binding_id.startswith(_HIDDEN_PREFIXES):
                continue
            # Two ids on one action (F3 and tab both switch a chart) collapse
            # to one row. Keyed on id, not action: Textual's built-in
            # `ctrl+q` shares the `quit` action with our `q`, and keying on
            # action dropped whichever arrived second.
            if binding_id in seen:
                continue
            seen.add(binding_id)

            display = self.app.get_key_display(binding)
            description = binding.description or binding.action.replace("_", " ")
            row = (display, description)
            if binding_id.startswith(_SPINE_PREFIX):
                spine.append(row)
            else:
                by_section.setdefault(section_for(binding_id), []).append(row)

        sections = [
            (heading, by_section[heading])
            for heading in SECTION_ORDER
            if by_section.get(heading)
        ]
        return spine, sections

    # -- rendering ----------------------------------------------------------

    def _title(self) -> str:
        return f"Key map — {self._mode_name()}"

    def _hint(self) -> str:
        preset = getattr(self.app, "_config", None)
        name = DEFAULT_PRESET
        if preset is not None:
            name = preset.keys.preset
        # One line at the dialog's 68 columns: the map is a reference card,
        # and a three-line footnote under it reads as content. `esc closes`
        # is not here because it rides in the border.
        lines = [
            f"^p finds any command · preset: {name} · remap in config.toml [keys]"
        ]
        if self._has_function_keys():
            lines.append(
                "F-keys: browsers claim F1/F3/F5 — use ^p, or tab to the "
                "chart tabs and ←/→"
            )
        return "\n".join(lines)

    def _has_function_keys(self) -> bool:
        """Whether this screen puts anything on an F-key.

        Asked of the bindings rather than hard-coded to the Explorer, so
        the note appears exactly where there is something it applies to
        and disappears if the last F-key ever moves. A web shell is a
        browser tab first: Chrome takes F1 for help, F3 for find and F5
        for reload, and none of the three reach the terminal.
        """
        under = self._under()
        if under is None:
            return False
        return any(
            key.startswith("f") and key[1:].isdigit()
            for key in under.active_bindings
        )
