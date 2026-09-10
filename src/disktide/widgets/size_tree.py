"""Tree[FSNode] subclass with size bars and lazy loading."""

from __future__ import annotations

from pathlib import Path

from textual import events
from textual.binding import Binding
from textual.widgets import Tree
from textual.widgets.tree import TreeNode
from rich.cells import cell_len
from rich.style import Style
from rich.text import Text

from disktide.metrics import (
    DEFAULT_METRIC,
    METRICS,
    metric_text,
    metric_value,
    metric_value_or_zero,
    normalize_metric,
)
from disktide.models.tree import FSNode
from disktide.domain.visualization import VisualDelta
from disktide.presentation.tui.viewmodels.visualization import (
    format_visual_delta,
    sparkline,
    visual_token,
)
from disktide.rendering import (
    bar_chars,
    denied_glyph,
    is_safe_rendering,
    link_arrow,
    partial_glyph,
)
from disktide.viz.colors import ink, ink_fill


class SizeTree(Tree[FSNode]):
    """A tree widget that displays filesystem nodes with size information."""

    DEFAULT_CSS = """
    SizeTree {
        width: 1fr;
        height: 1fr;
    }
    """

    BINDINGS = [
        # Textual binds `enter` to `select_cursor`, which posts
        # `Tree.NodeSelected` -- and `ExplorerScreen.on_tree_node_selected`
        # answers that by re-rooting the whole screen on the selected
        # directory. So `enter` was a second `i`, and the one thing a tree
        # is expected to do with the key -- open this row where it stands
        # -- was only ever on `space` and `right`, which is not where
        # anyone looks for it. Re-rooting stays on `i`; `enter` expands.
        #
        # This has to be declared on the widget rather than on the screen:
        # the tree holds focus, Textual resolves the focused widget's
        # bindings first, and a screen-level `enter` would never be
        # reached. A subclass entry replaces the base class's binding for
        # the same key -- `DOMNode._merge_bindings` walks the MRO
        # base-first and assigns rather than appending.
        Binding(
            "enter",
            "expand_or_collapse",
            "Expand/collapse",
            show=False,
            id="explorer.expand",
        ),
    ]

    BAR_WIDTH = 15
    """Cells the proportional bar uses when the row has room for it."""

    MIN_BAR_WIDTH = 6
    """Narrowest bar still worth drawing; below this the bar is dropped."""

    def __init__(
        self,
        root_node: FSNode | None = None,
        sort_key: str = "size",
        metric: str = DEFAULT_METRIC,
        **kwargs,
    ):
        label = root_node.name if root_node else "/"
        super().__init__(label, data=root_node, **kwargs)
        self._sort_key = sort_key
        self._metric = normalize_metric(metric)
        self._fs_root = root_node
        self._tree_nodes: dict[str, TreeNode[FSNode]] = {}
        self._live_update_count = 0
        self._visuals: dict[str, VisualDelta] = {}
        # The metric the attached visuals were computed in. A metric toggle
        # changes `_metric` immediately but the frame is rebuilt by a worker,
        # so until the new one lands its byte deltas must not be reprinted
        # with the new metric's formatter.
        self._visual_metric = self._metric
        self._mini_trends: dict[str, tuple[int | None, ...]] = {}
        self._diff_mode = False
        self._fitted_width = -1
        if root_node is not None:
            self._tree_nodes[root_node.path] = self.root
        if root_node:
            self.root.expand()

    @property
    def sort_key(self) -> str:
        return self._sort_key

    @sort_key.setter
    def sort_key(self, value: str) -> None:
        self._sort_key = value
        if self._fs_root:
            selected_path = self.selected_path
            self._fs_root.invalidate_sort()
            self.reload(self._fs_root, selected_path=selected_path)

    @property
    def metric(self) -> str:
        """Active canonical storage metric identifier."""
        return self._metric

    @metric.setter
    def metric(self, value: str) -> None:
        normalized = normalize_metric(value)
        if normalized not in METRICS or normalized == self._metric:
            return
        self._metric = normalized
        # When the quantitative sort is active the ordering depends on the
        # metric, so the tree has to be re-sorted (a reload) for the new
        # order to take effect. For name/mtime sorts only the labels change,
        # so refresh them in place to keep the cursor where it was.
        if self._sort_key not in ("name", "mtime") and self._fs_root is not None:
            self.reload(self._fs_root, selected_path=self.selected_path)
        else:
            self._refresh_labels()

    def _release_row_data(self) -> None:
        """Drop every row's `data` before the rows themselves are discarded.

        `Tree.clear()` does not delete the old rows -- it builds a fresh root
        and stops referring to them -- and a `TreeNode` graph is cyclic
        (`_tree`, `_parent`, `_children`), so what it leaves behind is an
        island the collector has to find. Until it does, every one of those
        rows still holds the `FSNode` it was drawn from, which is the whole
        previous tree: 282 rows kept 1,961 nodes alive in a small fixture and
        about 600 MB on a home-shaped one.

        Walking them and assigning `None` costs one pass over the rows the
        user could see -- dozens, occasionally thousands -- and turns "wait
        for a full collection, which with a tree resident is seconds" into
        "the refcount hits zero here". The rows are still garbage afterwards,
        but they are 282 small objects rather than a tree.
        """
        stack = [self.root]
        while stack:
            node = stack.pop()
            node.data = None
            stack.extend(node.children)

    def reload(self, root_node: FSNode, *, selected_path: str | None = None) -> None:
        """Reload the tree with a new root node."""
        restore_path = selected_path
        self._fs_root = root_node
        # Before `clear()`, which is the last moment these rows are reachable.
        self._release_row_data()
        self.clear()
        self._tree_nodes = {root_node.path: self.root}
        self.root.data = root_node
        self.root.set_label(self._make_label(root_node))
        self._populate_children(self.root, root_node)
        self.root.expand()
        # Tree.clear() leaves cursor_line at its previous value; the renderer
        # only clamps a now-out-of-range line to -1 ("no cursor"). Pin it to
        # the root so a rescan/sort/metric reload always shows a live cursor
        # instead of a frozen or invisible one. (Focus is restored by the
        # screen after a scan completes; see ExplorerScreen._on_scan_complete.)
        self.cursor_line = 0
        if restore_path and restore_path != root_node.path:
            # `notify=False`: restoring a cursor is not the user selecting
            # a row. `select_node` posts `Tree.NodeSelected`, which the
            # Explorer reads as "Enter on a directory" and answers by
            # drilling into it -- so every reload that happened to have a
            # directory under the cursor (a Diff toggle, a metric switch)
            # silently re-rooted the whole screen one level down.
            self.select_path(restore_path, notify=False)

    @property
    def root_path(self) -> str | None:
        """The path this tree is currently rooted at, if it has been loaded."""
        root = self._fs_root
        return None if root is None else root.path

    @property
    def selected_path(self) -> str | None:
        node = self.cursor_node
        if node is None or node.data is None:
            return None
        return node.data.path

    @property
    def diff_mode(self) -> bool:
        return self._diff_mode

    def set_visual_context(
        self,
        visuals: dict[str, VisualDelta] | None = None,
        mini_trends: dict[str, tuple[int | None, ...]] | None = None,
        *,
        diff_mode: bool = False,
    ) -> None:
        """Apply precomputed delta and trend labels without querying storage."""
        self._visuals = visuals or {}
        self._visual_metric = self._metric
        self._mini_trends = mini_trends or {}
        self._diff_mode = diff_mode
        self._refresh_labels()

    def set_path_trend(self, path: str, values: tuple[int | None, ...]) -> None:
        self._mini_trends[path] = values
        tree_node = self._tree_nodes.get(path)
        if tree_node is not None and tree_node.data is not None:
            tree_node.set_label(self._make_label(tree_node.data))
            self.refresh()

    def select_path(self, path: str, *, notify: bool = True) -> bool:
        """Expand the path ancestry and restore the cursor by stable identity.

        `notify` decides whether landing on the row also *selects* it. A
        click on a chart shape is a selection and wants the message; a
        cursor being put back where it was after a reload is not.
        """
        root = self._fs_root
        if root is None or root.find(path) is None:
            return False
        land = self.select_node if notify else self.move_cursor
        if path == root.path:
            land(self.root)
            return True

        current_fs = root
        current_tree = self.root
        try:
            relative = Path(path).relative_to(Path(root.path)).parts
        except ValueError:
            return False
        for part in relative:
            if not current_tree.children:
                self._populate_children(current_tree, current_fs)
            child_fs = next(
                (child for child in current_fs.children if child.name == part),
                None,
            )
            if child_fs is None:
                return False
            child_tree = self._tree_nodes.get(child_fs.path)
            if child_tree is None:
                return False
            # Expand the *parent*, whatever the child is: a node only gets
            # a line — and so only becomes selectable — once every ancestor
            # is open. Skipping this for file children left the cursor on
            # the root whenever the target was a file.
            current_tree.expand()
            current_fs = child_fs
            current_tree = child_tree
        # Expanding a node only invalidates the tree's line cache; a node
        # learns its line number when that cache is next rebuilt, and
        # `select_node` reads that number directly. Reading a property
        # backed by the cache rebuilds it here, so a node expanded a moment
        # ago resolves to its real line rather than -1 — which
        # `validate_cursor_line` would clamp straight back to the root.
        if self.last_line < 0:
            return False
        land(current_tree)
        return True

    @property
    def live_update_count(self) -> int:
        return self._live_update_count

    def begin_live(self, path: str) -> None:
        """Reset to a stable root placeholder before live updates arrive."""

        root = FSNode(
            name=Path(path).name or path,
            path=path,
            is_dir=True,
            allocated_size=0,
            own_allocated_size=0,
        )
        self._live_update_count = 0
        self.reload(root)

    def apply_live_update(
        self,
        root_node: FSNode,
        changed_nodes: tuple[FSNode, ...],
    ) -> None:
        """Apply changed directory checkpoints without rebuilding the tree."""

        if self._fs_root is None or self._fs_root.path != root_node.path:
            self.reload(root_node)
            self._live_update_count += 1
            return

        # Filtered to the rows that exist before anything is sorted. A frame
        # carries every directory that settled since the last one plus each
        # of their ancestor chains -- thousands of nodes, tens of thousands
        # when the frames are paced by this widget rather than by a clock --
        # and this tree has materialised the scan root's children and
        # whatever the user expanded, which is dozens. Sorting the full set
        # with a lambda key (one Python frame per item) to discard all but
        # those dozens measured a second inside one apply, and the apply runs
        # on the UI thread, which is the scan's thread too.
        materialised = self._tree_nodes
        changed_by_path = {
            node.path: node
            for node in changed_nodes
            if node.path in materialised
        }
        changed_by_path[root_node.path] = root_node
        self._fs_root = root_node

        for path, node in sorted(
            changed_by_path.items(),
            key=lambda item: (item[1].depth, item[0]),
        ):
            tree_node = materialised.get(path)
            if tree_node is None:
                continue
            tree_node.data = node
            tree_node.set_label(self._make_label(node))
            if tree_node is self.root or tree_node.is_expanded:
                self._sync_children(tree_node, node, changed_by_path)

        self._live_update_count += 1
        self.refresh()

    def action_expand_or_collapse(self) -> None:
        """Open or close the directory under the cursor, in place.

        Not `Tree.action_toggle_node`, which reads
        `_tree_lines[self.cursor_line]` -- and `cursor_line` is -1 when
        there is no cursor, which indexes the *last* row rather than
        raising. `enter` on a tree with no cursor would have toggled
        whatever happened to sit at the bottom. `cursor_node` is `None` in
        that state, which is the answer this wants.
        """
        node = self.cursor_node
        if node is None or not node.allow_expand:
            return
        node.toggle()

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[FSNode]) -> None:
        """Lazily load children when a node is expanded."""
        node = event.node
        fs_node = node.data
        if fs_node is None or not fs_node.is_dir:
            return

        # Only populate if not already done
        if not node.children:
            self._populate_children(node, fs_node)

    def _populate_children(self, tree_node: TreeNode[FSNode], fs_node: FSNode) -> None:
        """Add children to a tree node from an FSNode."""
        children = self._sorted(fs_node.children)
        for child in children:
            label = self._make_label(child)
            if child.is_dir:
                added = tree_node.add(label, data=child, allow_expand=True)
            else:
                added = tree_node.add_leaf(label, data=child)
            self._tree_nodes[child.path] = added

    def _sync_children(
        self,
        tree_node: TreeNode[FSNode],
        fs_node: FSNode,
        changed_by_path: dict[str, FSNode],
    ) -> None:
        existing = {
            child.data.path: child
            for child in tree_node.children
            if child.data is not None
        }
        desired = {child.path: child for child in fs_node.children}

        for path, child_node in tuple(existing.items()):
            if path in desired:
                continue
            self._unregister_subtree(child_node)
            child_node.remove()

        for child in self._sorted(fs_node.children):
            child_node = existing.get(child.path)
            if child_node is None:
                label = self._make_label(child)
                if child.is_dir:
                    child_node = tree_node.add(
                        label,
                        data=child,
                        allow_expand=True,
                    )
                else:
                    child_node = tree_node.add_leaf(label, data=child)
                self._tree_nodes[child.path] = child_node
                continue
            if child.path in changed_by_path:
                child_node.data = changed_by_path[child.path]
                child_node.set_label(self._make_label(changed_by_path[child.path]))

    def _unregister_subtree(self, tree_node: TreeNode[FSNode]) -> None:
        stack = [tree_node]
        while stack:
            current = stack.pop()
            if current.data is not None:
                self._tree_nodes.pop(current.data.path, None)
                # Same reason as `_release_row_data`: the row is about to be
                # removed, and a removed row is still a live reference to the
                # node it was showing until something collects the cycle it
                # sits in.
                current.data = None
            stack.extend(current.children)

    def _sorted(self, children: list[FSNode]) -> list[FSNode]:
        """Sort children based on current sort key.

        The quantitative ("size") sort follows the active metric: with the
        bar set to file count, directories are ordered by file count, not
        bytes — matching what the treemap and sunburst already do. Name and
        modified-time sorts are independent of the metric.
        """
        if self._sort_key == "name":
            return sorted(children, key=lambda n: n.name.lower())
        elif self._sort_key == "mtime":
            return sorted(children, key=lambda n: n.mtime, reverse=True)
        else:  # quantitative (default): largest of the active metric first
            return sorted(
                children,
                key=lambda n: (
                    -metric_value_or_zero(n, self._metric),
                    n.name.lower(),
                ),
            )

    def render_label(
        self, node: TreeNode[FSNode], base_style: Style, style: Style
    ) -> Text:
        """Put the bar's own background back on top of the cursor style.

        `Tree.render_label` applies the cursor/hover style over the whole
        label *after* the label's own spans, and a later Rich span's
        bgcolor wins.  That was free while the bar was `█` in a foreground
        colour; a bar painted as a background disappears on the selected
        row instead.  So the fill spans -- the only ones in a label that
        carry a bgcolor -- are re-applied afterwards, at the offset the
        expand glyph pushed them to.

        Nothing else about the cursor row changes: the name still goes
        white and bold, and the size column still takes the cursor's
        background, because those spans are foreground-only and the
        highlight is left to win over them exactly as before.
        """
        text = super().render_label(node, base_style, style)
        label = node._label
        offset = len(text.plain) - len(label.plain)
        for span in label.spans:
            span_style = span.style
            if isinstance(span_style, str):
                span_style = Style.parse(span_style)
            if span_style.bgcolor is None:
                continue
            text.stylize(span_style, offset + span.start, offset + span.end)
        return text

    def _make_label(self, node: FSNode) -> Text:
        """Create a rich label with name, a metric value, and a proportional bar.

        The active metric governs both the inline value and directory bar.
        """
        text = Text()

        # Name (directory, symlink, then plain file)
        if node.is_dir:
            text.append(f"{node.name}/", style=ink("dir"))
            if node.is_loop:
                text.append(" (loop)", style=ink("warning_strong"))
            elif node.vanished:
                # Dim, not a warning colour: the directory was removed while
                # the scan ran, which is a fact about the tree rather than
                # something the user has to act on.
                text.append(" (gone)", style=ink("muted"))
        elif node.is_symlink:
            text.append(node.name, style=ink("link"))
            if node.link_target:
                target = node.link_target
                if node.link_is_dir:
                    target = target.rstrip("/") + "/"
                text.append(f" {link_arrow()} {target}", style="dim")
            if node.link_broken:
                text.append(" (broken)", style=ink("error_strong"))
        else:
            text.append(node.name, style=ink("file"))

        value = metric_text(node, self._metric)
        text.append(f"  {value}", style="dim")

        visual = self._visuals.get(node.path) if self._diff_mode else None
        if visual is not None:
            token = visual_token(visual.state)
            text.append(
                f"  {format_visual_delta(visual, self._visual_metric)}",
                style=token.color,
            )

        trend = self._mini_trends.get(node.path)
        if trend:
            spark = sparkline(trend)
            if spark:
                text.append(f"  {spark}", style=ink("link_dim"))

        if node.is_hardlink:
            if node.is_hardlink_duplicate and node.hardlink_owner_path:
                owner = node.hardlink_owner_path.rsplit("/", 1)[-1]
                text.append(f"  [hardlink → {owner}]", style=ink("accent_dim"))
            else:
                text.append(
                    f"  [hardlink owner; {node.link_count} links]",
                    style=ink("accent_dim"),
                )

        if node.excluded:
            marker = "xdev" if node.filesystem_boundary else node.exclusion_reason
            text.append(f"  [{marker or 'excluded'}]", style=ink("warning_strong"))
        elif node.depth_limited:
            text.append("  [max-depth]", style=ink("warning_strong"))

        # Accessibility indicators (full denial vs partial). Rendered last,
        # but resolved here so the bar can reserve room for them: they sit
        # on the same rows as the bar and would otherwise push the percent
        # off the right edge.
        indicator: tuple[str, str] | None = None
        if node.error:
            indicator = (f" {denied_glyph()}", ink("error_strong"))
        elif node.is_dir and node.inaccessible_subtree_count > 0:
            # The count is the subtree aggregate (this directory's own
            # unreadable entries plus every descendant's, a denied subtree
            # counting 1 -- see scheduler._recalculate_directory), because a
            # row that showed only its direct count said nothing about the
            # dozens hidden below it and read as broken. Colour carries the
            # distinction the number no longer does: strong when some of it
            # is right here, dim when it is all further down.
            indicator = (
                f" {partial_glyph()} {node.inaccessible_subtree_count} hidden",
                ink("warning_strong")
                if node.inaccessible_count > 0
                else ink("warning_dim"),
            )
        elif node.inaccessible_count > 0:
            # Files, and directories whose subtree aggregate has not been
            # recalculated yet, still fall back to the direct count.
            indicator = (
                f" {partial_glyph()} {node.inaccessible_count} hidden",
                ink("warning_strong"),
            )

        # The access indicator goes on *before* the bar, so the bar and
        # the percent stay one right-aligned block. Appended after it, a
        # `◐ 12 hidden` badge shortened the bar and shifted the percent of
        # that row left of every other row's -- the percent column was a
        # column only on the rows with nothing to report.
        if indicator is not None:
            text.append(indicator[0], style=indicator[1])

        # Proportional bar for directories (share of the scan root total).
        # A vanished directory has nothing to show a share of, and a 0.0%
        # bar reads as a measurement rather than as an absence.
        if node.is_dir and not node.vanished:
            ratio = self._metric_ratio(node)
            if ratio is not None:
                self._append_share(text, node, ratio)

        return text

    def _append_share(self, text: Text, node: FSNode, ratio: float) -> None:
        """Append the bar and percent, degrading to whatever the row fits.

        The percent is the payload and is never truncated: the bar gives up
        cells first (down to `MIN_BAR_WIDTH`), then disappears, and only
        then does the percent itself go. Textual crops a label at the panel
        edge, so an over-wide tail would otherwise render as a fragment
        like "16." with the digits and sign shorn off.
        """
        percent = f"{ratio * 100:.1f}%"
        room = self._tail_room(node, text)

        if room is None:
            bar_width = self.BAR_WIDTH
        else:
            # "  " + bar + " " + percent
            bar_width = min(self.BAR_WIDTH, room - 3 - len(percent))
            # Right-align the whole block against the panel edge so the
            # percent lands in the same column on every row, whatever the
            # name, the delta or an access badge did to the row before it.
            used = (
                3 + bar_width + len(percent)
                if bar_width >= self.MIN_BAR_WIDTH
                else 2 + len(percent)
            )
            lead = max(0, room - used)
            if lead:
                text.append(" " * lead)

        if bar_width >= self.MIN_BAR_WIDTH:
            filled = int(ratio * bar_width)
            if is_safe_rendering():
                filled_ch, empty_ch = bar_chars()
                bar = filled_ch * filled + empty_ch * (bar_width - filled)
                text.append(f"  {bar} {percent}", style=ink("bar"))
            else:
                # Spaces under a background colour, not `█`/`░`: a browser
                # terminal has no cell-fitted glyph for either, and drew the
                # track 1.1 cells wide and 1.4 rows tall -- one row's bar
                # bleeding over the size text of the rows above and below.
                # A space with a background is the one primitive that fills
                # exactly one cell everywhere.
                text.append("  ")
                if filled:
                    text.append(" " * filled, style=ink_fill("bar"))
                if bar_width > filled:
                    text.append(
                        " " * (bar_width - filled), style=ink_fill("bar_track")
                    )
                text.append(f" {percent}", style=ink("bar"))
        elif room is None or room >= 2 + len(percent):
            text.append(f"  {percent}", style=ink("bar"))

    def _tail_room(self, node: FSNode, text: Text) -> int | None:
        """Cells left on `node`'s row for the bar and percent.

        The rendered row is the tree's guide indentation, plus the
        expand glyph `Tree.render_label` prepends to expandable nodes,
        plus the label built so far, access indicator included: that
        badge is appended before this runs, so its cells are already
        in `text` rather than having to be reserved out of band.

        Returns None when the widget has no usable width yet (before the
        first layout), so the caller emits the full-width tail and the
        resize handler re-fits it once a real width is known.
        """
        width = self.scrollable_content_region.width
        if width <= 0:
            return None
        root = self._fs_root
        depth = node.depth - root.depth if root is not None else node.depth
        indent = max(0, depth) * self.guide_depth
        # Only expandable nodes carry the glyph; both states are the same
        # width, but take the wider one so a toggle can never overflow.
        glyph = (
            max(cell_len(self.ICON_NODE), cell_len(self.ICON_NODE_EXPANDED))
            if node.is_dir
            else 0
        )
        return width - indent - glyph - text.cell_len

    def on_resize(self, event: events.Resize) -> None:
        """Re-fit labels whenever the usable width changes.

        Guarded on the width the labels were last fitted to: setting a
        label changes the tree's virtual size, which posts another resize,
        so an unguarded refresh would spin. The event's size covers the
        scrollbar columns, so the fitting width comes from the region.
        """
        width = self.scrollable_content_region.width
        if width <= 0 or width == self._fitted_width:
            return
        self._fitted_width = width
        self._refresh_labels()

    def _metric_ratio(self, node: FSNode) -> float | None:
        """Return `node`'s share of the scan root for the active metric.

        Returns None when there is no root or the root total is zero, so
        the caller skips the bar instead of dividing by zero.
        """
        root = self._fs_root
        if root is None:
            return None
        total = metric_value(root, self._metric)
        value = metric_value(node, self._metric)
        if total is None or value is None or total <= 0:
            return None
        return value / total

    def _refresh_labels(self) -> None:
        """Re-render every materialized node's label in place.

        Called after a metric change. Children that have not been lazily
        loaded yet need no work: they pick up the current metric from
        `_make_label` when they are first populated.
        """
        stack: list[TreeNode[FSNode]] = [self.root]
        while stack:
            tree_node = stack.pop()
            if tree_node.data is not None:
                tree_node.set_label(self._make_label(tree_node.data))
            stack.extend(tree_node.children)

    def rebuild_render_cache(self) -> None:
        """Re-render the labels after a global render decision moved.

        Textual's `Tree` holds each label as a `Text` built once by
        `_make_label` and handed over with `set_label`, so a `refresh()`
        redraws the same colours it was already drawing. The colour scheme
        and ASCII-safe rendering are both read inside `_make_label`, which
        makes this the one widget on the explorer that a theme change
        cannot reach on its own.
        """
        self._refresh_labels()

    def cycle_sort(self) -> str:
        """Cycle through sort options and return the new sort key."""
        options = ["size", "name", "mtime"]
        idx = options.index(self._sort_key)
        self.sort_key = options[(idx + 1) % len(options)]
        return self._sort_key

    def toggle_metric(self) -> str:
        """Cycle through all storage metrics; return the new metric."""
        index = METRICS.index(self._metric)
        self.metric = METRICS[(index + 1) % len(METRICS)]
        return self._metric
