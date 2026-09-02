"""The binding ids are a public contract, so they get a gate.

Once `[keys]` shipped, a binding id became something a user writes into their
own `config.toml`. That makes an id rename a breaking change in exactly the
way a CLI flag rename is, and it makes an id typo in the source a binding
nobody can remap. Both are cheap to catch here and expensive to catch in a
bug report.

The other half is that the presets have to stay honest: `classic` claims to
reproduce the v0.2.30 layout, and the only thing keeping that true is a test
that says so.
"""

from __future__ import annotations

import ast
import pathlib
import types

import pytest

from disktide.keys import ALL_IDS, DEFAULT_PRESET, PRESETS, resolve_keymap

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "disktide"


def _declared_bindings() -> list[tuple[str, str, str | None]]:
    """Every `Binding(...)` in the source, as (file, key, id).

    Read from the AST rather than by importing the screens: importing them
    pulls in the services, and this test is about the declarations.
    """
    found: list[tuple[str, str, str | None]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "Binding":
                continue
            key = None
            if node.args and isinstance(node.args[0], ast.Constant):
                key = node.args[0].value
            keywords = {k.arg: k.value for k in node.keywords}
            if key is None and isinstance(keywords.get("key"), ast.Constant):
                key = keywords["key"].value
            binding_id = None
            id_node = keywords.get("id")
            if isinstance(id_node, ast.Constant):
                binding_id = id_node.value
            found.append((str(path.relative_to(_SRC)), key, binding_id))
    return found


def test_every_declared_id_is_in_the_vocabulary():
    """A typo'd id is a binding that `[keys]` can never address."""
    unknown = [
        (where, key, binding_id)
        for where, key, binding_id in _declared_bindings()
        if binding_id is not None and binding_id not in ALL_IDS
    ]
    assert not unknown, (
        "these bindings carry an id that is not in disktide.keys.ALL_IDS, so "
        "`[keys]` cannot address them and the key map will skip them: "
        f"{unknown}"
    )


def test_no_id_is_declared_twice():
    """Two bindings sharing an id means `set_keymap` moves both."""
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for where, _key, binding_id in _declared_bindings():
        if binding_id is None:
            continue
        if binding_id in seen:
            duplicates.append(f"{binding_id} in {seen[binding_id]} and {where}")
        seen[binding_id] = where
    assert not duplicates, f"duplicate binding ids: {duplicates}"


@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_presets_only_name_real_bindings(preset):
    """A preset that names a retired id silently does nothing."""
    unknown = sorted(set(PRESETS[preset]) - ALL_IDS)
    assert not unknown, (
        f"preset {preset!r} maps ids that no longer exist: {unknown}. "
        "Either the binding was removed or its id was renamed — renaming "
        "needs an entry in disktide.keys._ID_ALIASES."
    )


def test_classic_preset_restores_the_moved_keys():
    """`classic` is the promise that v0.2.30 fingers still work.

    These are the nine moves the keymap change made. If one of them stops
    being reproducible, an existing user's `preset = "classic"` quietly
    stops meaning what it says.
    """
    keymap, problems = resolve_keymap("classic")
    assert not problems
    assert keymap["app.mode_cleanup"] == "c"
    assert keymap["app.settings"] == "question_mark"
    assert keymap["cleanup.review_plan"] == "d"
    assert keymap["cleanup.undo"] == "u"
    assert keymap["monitor.sampling"] == "s"
    assert keymap["fs.benchmark"] == "b"


def test_spine_is_what_the_source_declares():
    """The default preset overrides nothing — the source is the layout."""
    assert DEFAULT_PRESET == "spine"
    keymap, problems = resolve_keymap("spine")
    assert keymap == {}
    assert not problems


def test_a_bad_config_costs_one_binding_not_the_app():
    """A typo in `[keys]` must not stop the app from starting."""
    keymap, problems = resolve_keymap(
        "nonsense", {"cleanup.no_such_action": "j", "cleanup.undo": "k"}
    )
    # The unknown preset falls back, the unknown id is dropped, and the one
    # good override survives.
    assert keymap["cleanup.undo"] == "k"
    assert "cleanup.no_such_action" not in keymap
    assert len(problems) == 2


def test_the_three_consequence_mismatches_are_gone():
    """The safety-shaped half of the keymap change, stated as an assertion.

    `d`, `u` and `s` each meant something harmless on one screen and
    something with a consequence on another. Under the shipped layout no
    letter may carry both roles.
    """
    declared = {
        (where, key, binding_id)
        for where, key, binding_id in _declared_bindings()
        if binding_id
    }
    by_id = {binding_id: key for _where, key, binding_id in declared}

    # Cleanup's plan review and undo left `d` and `u`.
    assert by_id["cleanup.review_plan"] == "p"
    assert by_id["cleanup.undo"] == "z"
    # Explorer keeps the harmless meanings.
    assert by_id["explorer.diff"] == "d"
    assert by_id["explorer.up"] == "u"
    assert by_id["explorer.sort"] == "s"
    # Starting a background sampling host takes the shift form.
    assert by_id["monitor.sampling"] == "shift+s"
    # Benchmarking writes to the disk under test.
    assert by_id["fs.benchmark"] == "shift+b"
    # Archiving a monitor kept no key at all; it is palette-only.
    assert "monitor.archive" not in by_id


def test_every_binding_has_an_explicit_section():
    """A new binding must not fall silently into "Other actions".

    `section_for` defaults to OTHER so the key map can never crash on an
    unmapped id, which means a missing entry is invisible at runtime: the
    binding just quietly lands at the bottom of the last section. This is
    the only thing that catches it.
    """
    from disktide.keys import (
        CLEANUP_IDS,
        EXPLORER_IDS,
        FS_IDS,
        MONITOR_IDS,
        SECTIONS,
    )

    listed = set(EXPLORER_IDS + CLEANUP_IDS + MONITOR_IDS + FS_IDS)
    missing = sorted(listed - set(SECTIONS))
    assert not missing, (
        "these bindings have no section in disktide.keys.SECTIONS, so `?` "
        f"will bury them under 'Other actions': {missing}"
    )


def test_sections_are_all_orderable():
    """A section nobody ordered never renders."""
    from disktide.keys import SECTION_ORDER, SECTIONS

    unordered = sorted(set(SECTIONS.values()) - set(SECTION_ORDER))
    assert not unordered, (
        f"sections used but absent from SECTION_ORDER, so they are dropped "
        f"from the key map: {unordered}"
    )


def test_the_key_map_files_niche_keys_under_a_heading(tmp_path):
    """`g` was in the key map all along and still read as missing.

    A flat sixteen-row list hides a key about as well as no list does. The
    regression this guards is the interesting one: not "is `g` bound" but
    "is `g` findable", which means it has to sit under a heading with the
    other view keys rather than at position eleven of a single column.
    """
    import asyncio

    from textual.widgets import Static

    from disktide.app import DiskTideApp
    from disktide.config import load_config
    from tests.waiting import wait_for_explorer

    for index in range(4):
        (tmp_path / f"f{index}.txt").write_text("x" * (80 * (index + 1)))

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 44)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("question_mark")
            await pilot.pause()
            await asyncio.sleep(0.2)
            await pilot.pause()

            assert type(app.screen).__name__ == "KeymapScreen"
            blocks = []
            for static in app.screen.query(Static):
                rendered = static.render()
                text = getattr(rendered, "plain", str(rendered))
                blocks.append((set(static.classes), text))

            headings = [t for classes, t in blocks if "keymap-section" in classes]
            assert "Choose the view" in headings, headings

            # The rows block that follows the heading carries the niche keys.
            view_rows = next(
                text
                for index, (classes, text) in enumerate(blocks)
                if "keymap-rows" in classes
                and blocks[index - 1][1] == "Choose the view"
            )
            assert "Ring shape" in view_rows
            assert "Sunburst" in view_rows, view_rows

    asyncio.run(go())


def test_the_map_reads_the_screen_it_describes_in_either_push_order():
    """The bug that shipped: `?` listed only the app-wide keys.

    `push_screen` mounts a screen — and so runs `compose` — before it appends
    it to the stack, and whether the append wins that race differs between a
    real terminal and `run_test`. Reading `screen_stack[-2]` therefore meant
    "the screen below" under the harness and "the app's default screen" in
    front of a user, which is why every test passed while the shipped map
    showed nothing but `1 2 3 4 ? , q`. Both orders are pinned here because
    only one of them is reproducible in a test app.
    """
    from disktide.screens.keymap import KeymapScreen

    class _Detached(KeymapScreen):
        """A key map with a stack of its own, unmounted."""

        def __init__(self, stack):
            super().__init__()
            self._stack = stack

        @property
        def app(self):  # type: ignore[override]
            return types.SimpleNamespace(screen_stack=self._stack)

    default_screen, explorer = object(), object()

    screen = _Detached([default_screen, explorer])
    # `compose` ran first: this screen is not on the stack yet.
    assert screen._under() is explorer
    # ...and the other way round.
    screen._stack = [default_screen, explorer, screen]
    assert screen._under() is explorer


def test_the_app_hands_the_map_the_screen_it_was_opened_from(tmp_path):
    """`?` names the screen it describes, rather than "This screen".

    The title is the visible half of the bug above: a map that could not
    find the screen below it fell back to the app's default screen, whose
    class name is `Screen` and whose bindings are the app's alone.
    """
    import asyncio

    from disktide.app import DiskTideApp
    from disktide.config import load_config
    from tests.waiting import wait_for_explorer

    (tmp_path / "a.txt").write_text("x" * 100)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("question_mark")
            await pilot.pause()
            await asyncio.sleep(0.2)
            await pilot.pause()

            keymap = app.screen
            assert type(keymap).__name__ == "KeymapScreen"
            assert keymap._describes is app.get_screen("explorer")
            dialog = keymap.query_one("#keymap-dialog")
            assert dialog.border_title == "Key map — Explorer"

    asyncio.run(go())


def test_the_explorer_map_fits_a_modest_terminal(tmp_path):
    """The map is a reference card, and a card you scroll is a card you lose.

    Sections pack into columns precisely so the everyday screen's keys are
    on screen at once. 100x30 is the smallest terminal that is still a
    normal one; if a new binding pushes Explorer past it, either the section
    layout or the binding's place in it needs another look.
    """
    import asyncio

    from disktide.app import DiskTideApp
    from disktide.config import load_config
    from tests.waiting import wait_for_explorer

    (tmp_path / "a.txt").write_text("x" * 100)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("question_mark")
            await pilot.pause()
            await asyncio.sleep(0.2)
            await pilot.pause()

            dialog = app.screen.query_one("#keymap-dialog")
            assert dialog.virtual_size.height <= dialog.container_size.height, (
                "the Explorer key map no longer fits a 100x30 terminal: "
                f"{dialog.virtual_size.height} rows of content in "
                f"{dialog.container_size.height}"
            )

    asyncio.run(go())


# -- column packing ---------------------------------------------------------

_SPINE_ROWS = [
    ("1", "Explorer"),
    ("2", "Monitor"),
    ("3", "FS Overview"),
    ("4", "Cleanup"),
    ("?", "Keys"),
    (",", "Settings"),
    ("q", "Quit"),
]


def test_rows_pack_into_as_many_columns_as_fit():
    """One row per line is what buried `g` in a sixteen-row column."""
    from disktide.screens.keymap import column_count

    # Widest cell here is `3  FS Overview` — 14 columns, plus a 3-column gap.
    assert column_count(_SPINE_ROWS, 1, 67) == 4
    assert column_count(_SPINE_ROWS, 1, 40) == 2
    # Narrower than a single cell still renders, one row per line.
    assert column_count(_SPINE_ROWS, 1, 5) == 1


@pytest.mark.parametrize("width", [10, 20, 33, 40, 55, 67, 76])
def test_packing_never_drops_a_key_or_pads_a_line_end(width):
    """Every key survives every width, and no line ends in filler."""
    from disktide.screens.keymap import pack_rows

    lines = pack_rows(_SPINE_ROWS, 1, width).plain.split("\n")
    joined = " ".join(lines)
    for key, description in _SPINE_ROWS:
        assert f"{key}  {description}" in joined
    assert all(line == line.rstrip() for line in lines), lines


@pytest.mark.parametrize("width", [10, 20, 33, 40, 55, 67, 76])
def test_the_reported_height_is_the_height_it_renders(width):
    """The invariant that keeps a stray blank line out of the dialog.

    A container caches its arrangement, so a widget whose rendered height
    disagrees with the height it reported during layout leaves a gap that
    only a resize clears. The two are computed from the same column count
    for exactly this reason.
    """
    from disktide.screens.keymap import _Rows, pack_rows

    rows = _Rows(_SPINE_ROWS, 1)
    reported = rows.get_content_height(None, None, width)
    rendered = len(pack_rows(_SPINE_ROWS, 1, width).plain.split("\n"))
    assert reported == rendered
