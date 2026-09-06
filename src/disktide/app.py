"""Main Textual App with mode/screen management."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from textual import events
from textual.app import App
from textual.binding import Binding

from disktide import APP_NAME
from disktide.config import (
    AppConfig, cleanup_rule_directory, load_config, save_config,
    get_effective_paths, set_effective_paths,
)
from disktide.rendering import (
    bump_render_epoch,
    set_ring_shape,
    set_safe_rendering,
)
from disktide.glyphs import (
    use_web_safe_scrollbars,
    use_web_safe_toggle_buttons,
)
from disktide.commands import BindingCommands
from disktide.keys import MODE, resolve_keymap
from disktide.repositories import default_snapshot_repository
from disktide.repositories.snapshots import SnapshotRepository
from disktide.themes import ANSI_THEME, resolve_theme
from disktide.viz import cellgeom, colordepth
from disktide.viz.chrome import CHROME_THEMES
from disktide.viz.colors import SCHEMES, set_color_scheme
from disktide.widgets.confirm_modal import ConfirmModal
from disktide.widgets.monitor_editor import MonitorEditor, MonitorEditorResult

# The services and the mode screens are imported where they are first
# used, not here. Drawing the welcome screen needs none of them, and on a
# network filesystem the modules they pull in are the whole startup cost:
# a module file costs ~16ms to fault in from a cold NFS mount against
# ~0.4ms once the page cache holds it.
if TYPE_CHECKING:
    from disktide.domain.cleanup import CleanupActionKind
    from disktide.domain.monitor import MonitorDefinition
    from disktide.services.cleanup import CleanupService
    from disktide.services.monitor import MonitorService
    from disktide.services.scan import ScanService
    from disktide.services.visualization import VisualizationService


class DiskTideApp(App):
    """Interactive terminal disk usage explorer."""

    TITLE = APP_NAME
    SUB_TITLE = "Disk Usage Explorer"
    # The palette is what makes the third tier of the keymap free: every
    # action is reachable by name, so a binding no longer has to earn a key
    # to be discoverable. Turning it on is the prerequisite for the footer
    # being allowed to show only six verbs.
    ENABLE_COMMAND_PALETTE = True
    # Textual's own providers (themes, quit, screenshot) plus one that walks
    # this app's bindings, so every action is findable by name.
    COMMANDS = App.COMMANDS | {BindingCommands}

    # Textual draws a good deal of its stock chrome from the eighth-block
    # and quadrant glyphs -- `tall` on Input, Button, Checkbox, Switch and
    # Select, `hkey` on Collapsible and the command palette, `vkey` on the
    # footer's palette key and the help panel, `outer` on toasts. A browser
    # terminal has no monospace face for any of them, falls back to a
    # proportional font and draws them at that font's advance: on the
    # welcome screen in an Open OnDemand web shell a 72-cell Input drew its
    # `▔` row 129 cells wide and its `▁` row 86, tearing the widget apart.
    # These rules redraw the same geometry from the box-drawing block,
    # which is what a border may be made of now that no block element is
    # (see `disktide.glyphs` for the rule and the measurements behind it).
    #
    # They are here, and not in `assets/default.tcss`, because nothing
    # loads that file -- there is no `CSS_PATH` anywhere in `src/`, and all
    # of this app's real styling is inline `DEFAULT_CSS`. App-level CSS is
    # also what makes one rule per widget enough: Textual ranks every
    # `App.CSS` rule above every `DEFAULT_CSS` rule whatever the selectors
    # say, `!important` included, so a single rule covers a widget's
    # variants and states without restating them.
    #
    # Where two of these tie on specificity the later one wins, as in CSS,
    # so each widget reads base rule first, then its variants, then the
    # `:ansi` and compact forms that have to beat them.
    CSS = """
    /* Input: a full box either way, `tall` -> `solid`. */
    Input { border: solid $border-blurred; }
    Input:focus { border: solid $border; }
    Input.-invalid { border: solid $error 60%; }
    Input.-invalid:focus { border: solid $error; }
    Input.-textual-compact { border: none; }

    /* Button: Textual gives the default variant two bevel rows and no
       sides, and a full box under `:ansi`, where the terminal's own
       background cannot carry the shape. Both kept, both redrawn. */
    Button.-style-default {
        border-left: none;
        border-right: none;
        border-top: solid $surface-lighten-1;
        border-bottom: solid $surface-darken-1;
    }
    Button.-style-default.-primary {
        border-top: solid $primary-lighten-3;
        border-bottom: solid $primary-darken-3;
    }
    Button.-style-default.-success {
        border-top: solid $success-lighten-2;
        border-bottom: solid $success-darken-3;
    }
    Button.-style-default.-warning {
        border-top: solid $warning-lighten-2;
        border-bottom: solid $warning-darken-3;
    }
    Button.-style-default.-error {
        border-top: solid $error-lighten-2;
        border-bottom: solid $error-darken-3;
    }
    Button:ansi { border: solid $border-blurred; }
    Button:ansi.-primary { border: solid $primary; }
    Button:ansi.-success { border: solid $success; }
    Button:ansi.-warning { border: solid $warning; }
    Button:ansi.-error { border: solid $error; }
    Button.-textual-compact { border: none; }

    /* Checkbox and RadioButton (ToggleButton), and Switch. */
    ToggleButton { border: solid $border-blurred; }
    ToggleButton:focus { border: solid $border; }
    ToggleButton.-textual-compact { border: none; }
    Switch { border: solid $border-blurred; }
    Switch:focus { border: solid $border; }

    /* Select: the closed control, its dropdown, and any other OptionList
       (the command palette's results list is one). */
    SelectCurrent { border: solid $border-blurred; }
    Select:focus > SelectCurrent { border: solid $border; }
    SelectCurrent.-textual-compact { border: none; }
    SelectOverlay { border: solid $border-blurred; }
    OptionList { border: solid $border-blurred; }
    OptionList:focus { border: solid $border; }
    OptionList.-textual-compact { border: none; }

    /* Collapsible's title rule. */
    Collapsible { border-top: solid $background; }
    Collapsible:ansi { border-top: solid ansi_blue; }

    /* The footer's command-palette key is fenced off by a `vkey` rule. */
    FooterKey.-command-palette { border-left: solid $foreground 20%; }

    /* Toast: `outer` puts a coloured bar down the left edge, out of the
       quadrants. `solid` draws the same edge as a box-drawing rule. */
    Toast.-information { border-left: solid $success; }
    Toast.-warning { border-left: solid $warning; }
    Toast.-error { border-left: solid $error; }

    /* The command palette. `hkey` reserves a column on each side and
       draws it blank, so the sides stay `blank` and only the rows that
       were `▔`/`▁` become `solid`. */
    CommandList { border-bottom: solid black; }
    CommandPalette #--input {
        border-top: solid black 50%;
        border-bottom: solid black 50%;
        border-left: blank black 50%;
        border-right: blank black 50%;
    }
    CommandPalette #--input.--list-visible { border-bottom: none; }
    CommandPalette LoadingIndicator { border-bottom: solid $border; }

    /* Textual's own help and key panels, reachable from the palette. */
    HelpPanel { border-left: solid $foreground 30%; }
    KeyPanel { border-left: solid $foreground 30%; }
    """

    BINDINGS = [
        # The four mode digits are one control, so they are one footer group.
        # Textual renders grouped keys bare and puts a single "Mode" label
        # after the run — which is exactly what the old
        # "[1]Explorer [2]Monitor [3]FS-Overview" description was drawing by
        # hand. Keep the four adjacent: Footer groups with itertools.groupby,
        # which only ever sees consecutive runs.
        Binding(
            "1", "switch_mode('explorer')", "Explorer",
            show=True, group=MODE, id="app.mode_explorer",
        ),
        Binding(
            "2", "switch_mode('monitor')", "Monitor",
            show=True, group=MODE, id="app.mode_monitor",
        ),
        Binding(
            "3", "switch_mode('fs_overview')", "FS Overview",
            show=True, group=MODE, id="app.mode_fs_overview",
        ),
        Binding(
            "4", "switch_mode('cleanup')", "Cleanup",
            show=True, group=MODE, id="app.mode_cleanup",
        ),
        # `?` means help everywhere else in the world; it used to open
        # Settings, which is why the app had no key map at all. Settings
        # takes `,`, the near-universal preferences key, freed by retiring
        # the cell-aspect nudges to the Settings screen that already has a
        # field for them.
        Binding(
            "question_mark", "show_keymap", "Keys",
            show=True, key_display="?", id="app.keymap",
        ),
        Binding(
            "comma", "push_screen('settings')", "Settings",
            show=False, key_display=",", id="app.settings",
        ),
        Binding("q", "quit", "Quit", show=True, id="app.quit"),
    ]

    def __init__(
        self,
        scan_path: str | None = None,
        config: AppConfig | None = None,
        show_welcome: bool = False,
        snapshot_repository: SnapshotRepository | None = None,
        **kwargs,
    ):
        # The config is read before `super().__init__` because
        # `ansi_color` is a constructor argument and the theme decides it:
        # once the App exists, Textual has already installed (or not) the
        # filter that rewrites ANSI colour names into RGB, and that is
        # exactly what an ANSI theme must not have happen to it.
        # Before any screen exists, because both of these replace class
        # attributes read at render time: the first scrollbar to paint
        # would otherwise draw a stock eighth-block thumb end, and the
        # first Checkbox a pair of half blocks. Cheap and idempotent, so
        # every entry point that builds an app -- CLI, `run_test`,
        # `textual-serve` -- gets them.
        use_web_safe_scrollbars()
        use_web_safe_toggle_buttons()
        self._config = config or load_config()
        self._color_depth = colordepth.active_color_depth()
        saved_theme = resolve_theme(self._config.ui.color_theme)
        # A 16-colour terminal is rendered with the theme designed for
        # sixteen colours, whatever is saved. Not written back: the depth
        # is a property of where the session is being read, and the next
        # one may be somewhere else.
        self._forced_ansi = (
            self._color_depth.value == "16" and saved_theme != ANSI_THEME
        )
        self._session_theme = ANSI_THEME if self._forced_ansi else saved_theme
        ansi_only = "NO_COLOR" in os.environ
        if ansi_only or self._session_theme == ANSI_THEME:
            # Names go out as names. Without this Textual converts every
            # ANSI colour to RGB through its own ANSI theme on the way to
            # the terminal, which is the whole thing the ANSI theme exists
            # to avoid.
            kwargs["ansi_color"] = True
        super().__init__(**kwargs)
        self._ansi_only = ansi_only
        # Registered here, before anything can be mounted: `App.theme` is a
        # validated reactive and assigning a name Textual has not seen
        # raises rather than falling back.
        for theme in CHROME_THEMES:
            self.register_theme(theme)
        # Textual's animations cost ~320ms of the ~420ms a viz tab switch
        # took, and each one is a burst of frames down an ssh pipe — this
        # tool's home is a login/compute node, not a local terminal.
        # TEXTUAL_ANIMATIONS is the user's own knob, so any value set there
        # keeps whatever Textual resolved from it.
        if not os.environ.get("TEXTUAL_ANIMATIONS"):
            self.animation_level = "none"
        self._scan_path = str(Path(scan_path).resolve()) if scan_path else None
        self._snapshot_repository = (
            snapshot_repository or default_snapshot_repository()
        )
        # Backing fields for the lazy service properties below.
        self.__scan_service: ScanService | None = None
        self.__monitor_service: MonitorService | None = None
        self.__visualization_service: VisualizationService | None = None
        self.__cleanup_service: CleanupService | None = None
        self.__cleanup_safe_action: CleanupActionKind | None = None
        self._show_welcome = show_welcome
        # The mode screens do not exist until a path has been chosen, and
        # the keys that reach them are bound from mount. `check_action`
        # below reads this to keep 1/2/3/4 and `,` off the welcome screen.
        self._modes_installed = False
        self._explorer = None
        # Ensures the "running without persistence" warning is only shown
        # once per session, no matter how many times we check.
        self._warned_degraded = False

    # Services build themselves on first touch. The attribute names are
    # unchanged, so screens and tests still reach them as `_scan_service`
    # and friends without knowing they were deferred.

    @property
    def _scan_service(self) -> ScanService:
        if self.__scan_service is None:
            from disktide.services.scan import ScanService

            self.__scan_service = ScanService()
        return self.__scan_service

    @property
    def _monitor_service(self) -> MonitorService:
        if self.__monitor_service is None:
            from disktide.services.monitor import MonitorService

            self.__monitor_service = MonitorService(
                self._snapshot_repository,
                scan_service=self._scan_service,
                host_type="tui",
                soft_budget_bytes=self._config.monitor.database_soft_budget,
                hard_budget_bytes=self._config.monitor.database_hard_budget,
                event_mode=self._config.monitor.event_mode,
            )
        return self.__monitor_service

    @property
    def _visualization_service(self) -> VisualizationService:
        if self.__visualization_service is None:
            from disktide.services.visualization import VisualizationService

            self.__visualization_service = VisualizationService(
                self._snapshot_repository
            )
        return self.__visualization_service

    @property
    def _cleanup_service(self) -> CleanupService:
        if self.__cleanup_service is None:
            from disktide.cleanup.actions import QuarantineExecutor
            from disktide.cleanup.rules import get_rule_by_name
            from disktide.services.cleanup import CleanupService

            self.__cleanup_service = CleanupService(
                self._snapshot_repository,
                quarantine=QuarantineExecutor(
                    retention_days=(
                        self._config.cleanup.quarantine_retention_days
                    ),
                    max_bytes=self._config.cleanup.quarantine_max_bytes,
                ),
                rule_provider=lambda name: get_rule_by_name(
                    name,
                    disabled_packs=self._config.cleanup.disabled_rule_packs,
                    user_directory=cleanup_rule_directory(),
                ),
            )
        return self.__cleanup_service

    @property
    def _cleanup_safe_action(self) -> CleanupActionKind:
        if self.__cleanup_safe_action is None:
            from disktide.domain.cleanup import CleanupActionKind

            self.__cleanup_safe_action = (
                CleanupActionKind.TRASH
                if self._config.cleanup.prefer_trash
                else CleanupActionKind.QUARANTINE
            )
        return self.__cleanup_safe_action

    def apply_color_theme(self, name: str) -> str:
        """Apply a colour theme to both halves of the UI, and say which landed.

        The charts read `viz.colors` and the chrome reads Textual's theme
        registry, and nothing connects the two but this call — so the
        settings picker and startup go through here rather than each
        remembering to do both. Returns the resolved key so a caller with a
        legacy or hand-typed name can write back what was actually used.

        Under `NO_COLOR` the Textual theme is left alone: the app is already
        in ansi mode, where every hex the theme carries is discarded, and
        switching it would only churn the CSS.
        """
        resolved = resolve_theme(name)
        set_color_scheme(resolved)
        if not self._ansi_only:
            self.theme = SCHEMES[resolved].textual_theme
        return resolved

    def on_mount(self) -> None:
        # Before any screen is pushed: a screen caches its active bindings on
        # mount, so a keymap applied later would leave the first screen on the
        # declared keys.
        self._apply_keymap()

        # The saved key is normalised, then the *session's* theme is
        # applied — the two differ only when the terminal turned out to
        # have sixteen colours, and the saved one has to survive that.
        self._config.ui.color_theme = resolve_theme(self._config.ui.color_theme)
        self.apply_color_theme(self._session_theme)
        set_safe_rendering(self._config.ui.safe_rendering)
        # The environment wins over the config so a shape can be asked for
        # per-launch — which is the whole of how the two are compared,
        # including by the README capture pipeline.
        self._config.ui.ring_shape = set_ring_shape(
            os.environ.get("DISKTIDE_RING_SHAPE") or self._config.ui.ring_shape
        )
        cellgeom.set_configured_aspect(self._config.ui.cell_aspect)

        # Connect the DB up front so a fallback to an in-memory database is
        # detected before the session starts. The warning is
        # emitted after the first screen is pushed (below), so the toast
        # lands on a visible screen rather than the pre-mount default one.
        self._snapshot_repository.connect()

        if self._show_welcome:
            from disktide.screens.welcome import WelcomeScreen

            paths = get_effective_paths(self._config)
            cwd = os.getcwd()
            recent = self._snapshot_repository.recent_paths(limit=5)

            self.push_screen(
                WelcomeScreen(
                    cwd_path=cwd,
                    saved_path=paths.default_scan_path,
                    last_visited_path=paths.last_visited_path,
                    recent_paths=recent,
                ),
                callback=self._on_welcome_result,
            )
        else:
            self._launch_explorer(self._scan_path or str(Path(".").resolve()))

        if (
            self._config.monitor.auto_start_in_tui
            and self._snapshot_repository.status.writable
        ):
            try:
                self._monitor_service.start_session(host_type="tui")
            except Exception:
                pass

        self._warn_if_degraded()
        self._explain_ansi_fallback()

    def on_resize(self, event: events.Resize) -> None:
        """Take the terminal's pixel size from an in-band resize report.

        Terminals that support mode 2048 send their pixel size with every
        resize, which is the only measurement available in a good few of
        them: VS Code's integrated terminal and the xterm.js web shells
        leave `TIOCGWINSZ`'s pixel fields at zero forever but will happily
        report in band. SIGWINCH-driven resizes carry `pixel_size=None`,
        so this fires only when there is something real to learn.

        Textual dispatches `_on_resize` and `on_resize` both, so the
        built-in one still runs; this only adds to it. The charts are not
        touched directly — a widget mid-paint must never have its layout
        swapped underneath it (see `SunburstView._ensure_layout`) — so a
        changed aspect goes out as a render-epoch bump and each chart
        rebuilds on its own deferred path.
        """
        pixel_size = event.pixel_size
        if pixel_size is None or pixel_size.width <= 0 or pixel_size.height <= 0:
            return
        before = cellgeom.detect_cell_aspect()
        if not cellgeom.report_pixel_size(event.size, pixel_size):
            return
        if abs(cellgeom.detect_cell_aspect() - before) > cellgeom.ASPECT_EPSILON:
            bump_render_epoch()

    def _warn_if_degraded(self) -> None:
        """Warn once if the database fell back to memory.

        Surfaced on every launch path so the user always learns that
        snapshots/history won't be persisted this session, regardless of
        whether they came in through the welcome screen or straight into
        the explorer.
        """
        status = self._snapshot_repository.status
        if status.writable or self._warned_degraded:
            return
        self._warned_degraded = True
        if status.read_only:
            message = (
                "The snapshot database is read-only. Existing history can "
                "be viewed, but new snapshots will not be saved."
            )
        else:
            message = (
                "The storage database is unavailable. Snapshots and history "
                "won't be saved this session; file exploration still works."
            )
        self.notify(
            message,
            title="Running without persistence",
            severity="warning",
            timeout=10,
        )

    def _explain_ansi_fallback(self) -> None:
        """Say once that the terminal, not the config, picked this theme.

        Without it the user sees a theme they did not choose and has no
        way to find out why — which is the same complaint the 16-colour
        rendering exists to answer, one level up.
        """
        if not self._forced_ansi:
            return
        self.notify(
            "16-colour terminal detected: using the ANSI theme. "
            "`disktide doctor` explains how to get full colour.",
            title="Colour depth",
            timeout=8,
        )

    def _on_welcome_result(self, result: tuple[str, bool] | None) -> None:
        """Callback from WelcomeScreen with the chosen path and save flag."""
        if result is None or self._exit:
            return
        path, save_default = result
        paths = get_effective_paths(self._config)
        changed = False
        if save_default and paths.default_scan_path != path:
            paths.default_scan_path = path
            changed = True
        if paths.last_visited_path != path:
            paths.last_visited_path = path
            changed = True
        if changed:
            set_effective_paths(self._config, paths)
            try:
                save_config(self._config)
            except OSError:
                pass
        self._launch_explorer(path)

    def _save_last_visited(self, path: str) -> None:
        """Persist the last-visited path to config."""
        paths = get_effective_paths(self._config)
        if paths.last_visited_path == path:
            return
        paths.last_visited_path = path
        set_effective_paths(self._config, paths)
        try:
            save_config(self._config)
        except OSError:
            pass

    def _launch_explorer(self, scan_path: str) -> None:
        """Install mode screens and push the explorer."""
        # Deferred to here rather than module scope: between them these
        # five screens reach about a third of the app's import graph (the
        # monitor screen alone reaches plotext through the trend chart),
        # and none of it is needed until the user has picked a path.
        from disktide.screens.cleanup import CleanupScreen
        from disktide.screens.explorer import ExplorerScreen
        from disktide.screens.fs_overview import FSOverviewScreen
        from disktide.screens.monitor import MonitorScreen
        from disktide.screens.settings import SettingsScreen

        self._scan_path = scan_path

        self._explorer = ExplorerScreen(
            self._scan_path,
            config=self._config,
            scan_service=self._scan_service,
            visualization_service=self._visualization_service,
            monitor_service=self._monitor_service,
        )
        self._cleanup = CleanupScreen(
            service=self._cleanup_service,
            safe_action=self._cleanup_safe_action,
            config=self._config,
        )
        self._monitor = MonitorScreen(
            service=self._monitor_service,
            config=self._config,
            visualization_service=self._visualization_service,
            root_path=self._scan_path,
            selected_path=self._scan_path,
        )
        self._fs_overview = FSOverviewScreen()

        self.install_screen(self._explorer, name="explorer")
        self.install_screen(self._cleanup, name="cleanup")
        self.install_screen(self._monitor, name="monitor")
        self.install_screen(self._fs_overview, name="fs_overview")
        self.install_screen(
            SettingsScreen(
                self._config,
                repository=self._snapshot_repository,
                scan_path=self._scan_path,
            ),
            name="settings",
        )

        self._modes_installed = True
        self.push_screen("explorer")

    def check_action(
        self, action: str, parameters: tuple[object, ...]
    ) -> bool | None:
        """Hide the mode keys until there are modes to switch to.

        `1`/`2`/`3`/`4` and `,` are app-level bindings, so they are live
        from mount, but the screens they name are only installed once the
        welcome screen has produced a path. Pressing one before that raised
        out of the key handler -- `No screen called 'explorer' installed`,
        or an attribute error for the explorer that does not exist yet --
        and took the whole app down with it. Returning False both refuses
        the key and takes it out of the footer, so the welcome screen
        advertises only what it can do.
        """
        if action == "switch_mode" and not self._modes_installed:
            return False
        if (
            action == "push_screen"
            and not self._modes_installed
            and parameters
            and parameters[0] == "settings"
        ):
            return False
        return True

    def open_monitor_setup(
        self,
        default_path: str,
        *,
        on_created: Callable[[MonitorDefinition], None] | None = None,
    ) -> None:
        """Open the shared monitor editor and persist its result."""

        def _on_result(result: MonitorEditorResult | None) -> None:
            if result is None:
                return
            created: MonitorDefinition | None = None
            try:
                warnings = self._monitor_service.definition_warnings(
                    result.definition
                )
                created = self._monitor_service.create_monitor(result.definition)
                for warning in warnings:
                    self.notify(warning, severity="warning", timeout=6)
                if result.capture_now and created.id is not None:
                    if not self._monitor_service.session_running:
                        self._monitor_service.start_session(host_type="tui")
                    self._monitor_service.run_monitor_now(created.id)
            except Exception as exc:
                self.notify(
                    f"{type(exc).__name__}: {exc}",
                    title="Monitor setup failed",
                    severity="error",
                    timeout=8,
                )
            if created is not None and on_created is not None:
                on_created(created)

        self.push_screen(
            MonitorEditor(config=self._config, default_path=default_path),
            callback=_on_result,
        )

    def action_quit(self) -> None:
        """Gate quit behind a y/n prompt to avoid accidental exits.

        On confirm, the real teardown runs in `_perform_quit`. The
        scanner checks the service cancellation token at every directory
        boundary, so the worker thread bails out quickly. Once the TUI
        has torn down, the terminal side cancels the scan, closes the
        database, and exits without printing anything — see
        disktide.__main__.
        """
        # If a modal (e.g. the quit prompt itself) is already on top,
        # ignore repeated `q` presses so we don't stack prompts.
        if isinstance(self.screen, ConfirmModal):
            return

        def _on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self._perform_quit()

        self.push_screen(
            ConfirmModal(
                message=f"Quit {APP_NAME}?",
                title="Quit",
                confirm_keys=("q",),
            ),
            callback=_on_confirm,
        )

    def _perform_quit(self) -> None:
        """Save config, cancel active scans, then exit the app."""
        try:
            save_config(self._config)
        except OSError:
            pass
        # Reached through the backing fields, not the properties: quitting
        # from the welcome screen must not build a service — and pay for
        # its imports — purely to shut it down again.
        if self.__monitor_service is not None:
            try:
                self.__monitor_service.stop_session(wait=False)
            except Exception:
                pass
        try:
            self._explorer.cancel_active_scan()
        except AttributeError:
            if self.__scan_service is not None:
                self.__scan_service.cancel_all()
        self.exit()

    def _apply_keymap(self) -> None:
        """Resolve `[keys]` from the config and hand it to Textual.

        A bad preset name or a stale binding id costs the user that one
        binding and a toast, not the session — the app has to start even if
        the config is out of date with the release.
        """
        keymap, problems = resolve_keymap(
            self._config.keys.preset, self._config.keys.overrides
        )
        if keymap:
            self.set_keymap(keymap)
        for problem in problems:
            self.notify(problem, severity="warning", timeout=8)

    def action_show_keymap(self) -> None:
        """Open the key map — every binding on the current screen, grouped."""
        from disktide.screens.keymap import KeymapScreen

        # Pressing `?` again from inside the key map should close it rather
        # than stack a second copy.
        if isinstance(self.screen, KeymapScreen):
            self.pop_screen()
            return
        # The screen is handed over rather than looked up from inside the
        # map: `push_screen` composes before it appends, so the map cannot
        # read its own position in the stack. This is the only moment at
        # which "the screen the user pressed `?` on" is unambiguous.
        self.push_screen(KeymapScreen(self.screen))

    def action_switch_mode(self, mode: str) -> None:
        """Switch between explorer/cleanup/monitor/fs_overview modes."""
        if not self._modes_installed or self._explorer is None:
            # `check_action` already refuses the keys; this covers a caller
            # that reaches the action some other way.
            self.notify(
                "Choose a directory first.", severity="warning",
            )
            return
        if mode == "explorer":
            self.switch_screen("explorer")
        elif mode == "cleanup":
            if not self._config.ui.show_cleanup:
                self.notify(
                    "Cleanup mode is disabled. Enable it in Settings (,).",
                    severity="warning",
                )
                return
            # Pass current root from explorer to cleanup
            if self._explorer._root is not None:
                self._cleanup.set_root(self._explorer._root)
            self.switch_screen("cleanup")
        elif mode == "monitor":
            root_path = (
                self._explorer._root.path
                if self._explorer._root is not None
                else self._explorer._scan_path
            )
            selected_path = self._explorer.selected_path
            self._monitor.set_navigation_context(root_path, selected_path)
            self.switch_screen("monitor")
        elif mode == "fs_overview":
            self.switch_screen("fs_overview")
