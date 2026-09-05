"""Runtime invariant checks for the all-tree scan scheduler.

`TreeScanScheduler` keeps a live map of in-flight directories keyed by path,
and addresses each directory's entries by *position*: `_DirectoryState`
carries a `next_child_index` cursor into `node.children`, and every child
carries a `parent_index` back into its parent's list. Those positions are
only meaningful while the list they point into holds still, and the scheduler
both appends to and sorts that list as a scan progresses.

Nothing in the ordinary assertions notices when a position goes stale --
totals still add up, so a corrupted cursor surfaces much later as a
`KeyError` on a parent that has already left `_states`. These checks close
that gap by validating the structure itself on every mutation.

Install with :func:`install`; `tests/conftest.py` does it for every test, so
any test that runs a scan is covered whether or not it was written with the
scheduler in mind.
"""

from __future__ import annotations

from collections import Counter

from disktide.scanner import scheduler as scheduler_module


class SchedulerInvariantViolation(AssertionError):
    """A scheduler structural invariant broke mid-scan."""


#: Methods wrapped by :func:`install`. Each mutates scheduler state, so the
#: invariants are re-checked once each one returns.
_WRAPPED = (
    "_apply_result",
    "_apply_entry_chunk",
    "_mark_settled",
    "_next_child_job",
)

#: Everything `_recalculate_directory` writes, and so everything I7 has to
#: agree about.
_RECALCULATED = (
    "size",
    "allocated_size",
    "file_count",
    "dir_count",
    "inaccessible_count",
    "inaccessible_subtree_count",
    "vanished_count",
    "vanished_subtree_count",
    "denied_dir_subtree_count",
    "partial_dir_subtree_count",
    "excluded_subtree_count",
    "depth_limited_subtree_count",
)


def _check_applied_aggregates(state) -> None:
    """I7: an applied directory's aggregates are its children's, exactly.

    `_apply_whole_directory` installs the node the worker built without
    recalculating it, on the grounds that the worker already summed these
    same children and nothing has touched them since. That is the one thing
    the fast path assumes and nothing else checks: every other invariant is
    about structure, and a directory carrying a stale total still points at
    all the right children. So the sum is redone here, from the node's
    current child list, and has to come back with the same twelve numbers.

    It holds for the streaming path too -- that one calls
    `_recalculate_directory` itself -- so it is checked on every result
    rather than only on the fast one.
    """

    node = state.node
    if not node.is_dir:
        return
    mirror = node.shallow_copy()
    recalculate = scheduler_module._recalculate_directory
    recalculate(mirror, state.direct_inaccessible, state.direct_vanished)
    for name in _RECALCULATED:
        applied = getattr(node, name)
        expected = getattr(mirror, name)
        if applied != expected:
            raise SchedulerInvariantViolation(
                f"[_apply_result] {node.path} carries {name}={applied!r} "
                f"where its children sum to {expected!r}"
            )


def _check_allocated_availability(state) -> None:
    """I8: `None` allocated bytes come from the platform, never from a gap.

    "Unavailable" means exactly one thing -- this platform has no
    `st_blocks`, so nothing in the tree has a number. Every other way of not
    reading a directory (denied, unstat-able, depth-limited, excluded,
    vanished, cancelled) contributes a number and reports itself through the
    coverage counters instead, because `_propagate` carries a `None` from
    any one node all the way to the scan root: one `chmod 000` directory
    used to blank the whole scan's Allocated and Unique.

    So a directory that has been applied and carries `None` has to have at
    least one entry that carries `None` too. An applied directory with none
    of those is the regression this exists to catch.
    """

    node = state.node
    if not node.is_dir or node.allocated_size is not None:
        return
    for child in node.children:
        value = (
            child.allocated_size if child.is_dir else child.own_allocated_size
        )
        if value is None:
            return
    raise SchedulerInvariantViolation(
        f"[_apply_result] {node.path} reports allocated_size=None where all "
        f"{len(node.children)} of its entries carry a number -- a coverage "
        "gap has been reported as an unavailable metric"
    )


def _check(scheduler, where: str) -> None:
    states = scheduler._states
    for path, state in list(states.items()):
        parent_path = state.job.parent_path

        # I3: a settled directory is dropped from the live map, so finding one
        # still registered means the pop was skipped or the state came back.
        # The scan root is the documented exception -- `_mark_settled` returns
        # before popping it and `scan()` reads the finished tree back out of
        # `_states[root_path]`.
        if state.settled and parent_path is not None:
            raise SchedulerInvariantViolation(
                f"[{where}] settled state still live: {path}"
            )

        # I4: one readdir yields a path once, so a repeat means an entry was
        # written into the wrong slot or appended twice.
        seen: set[str] = set()
        for child in state.node.children:
            if child.path in seen:
                raise SchedulerInvariantViolation(
                    f"[{where}] {path} lists {child.path} twice"
                )
            seen.add(child.path)

        # I5: the cursor may reach the end of the list but never pass it.
        if state.next_child_index > len(state.node.children):
            raise SchedulerInvariantViolation(
                f"[{where}] cursor past end for {path}: "
                f"{state.next_child_index} > {len(state.node.children)}"
            )

        if parent_path is None:
            continue

        # I1: a live directory's ancestors must still be live -- every walk up
        # the chain (`_propagate`, `_ensure_mutable`, `_record_changed`)
        # indexes `_states` unguarded and dies on the whole scan otherwise.
        if parent_path not in states:
            raise SchedulerInvariantViolation(
                f"[{where}] orphaned state {path}: parent {parent_path} "
                "already left the live map"
            )

        # I2: parent_index must still address this exact child.
        parent = states[parent_path]
        if state.parent_index is None:
            raise SchedulerInvariantViolation(
                f"[{where}] {path} has no parent_index"
            )
        if state.parent_index >= len(parent.node.children):
            raise SchedulerInvariantViolation(
                f"[{where}] {path} parent_index {state.parent_index} is out of "
                f"range for {parent_path} ({len(parent.node.children)} entries)"
            )
        addressed = parent.node.children[state.parent_index]
        if addressed.path != path:
            raise SchedulerInvariantViolation(
                f"[{where}] {path} parent_index {state.parent_index} addresses "
                f"{addressed.path} instead"
            )


def install(monkeypatch) -> None:
    """Wrap the scheduler's mutators so every state change is validated."""

    for name in _WRAPPED:
        original = getattr(scheduler_module.TreeScanScheduler, name)

        def make(name: str, original):
            def wrapper(self, *args, **kwargs):
                result = original(self, *args, **kwargs)
                _check(self, name)
                if name == "_apply_result":
                    _check_applied_aggregates(result)
                    _check_allocated_availability(result)
                return result

            return wrapper

        monkeypatch.setattr(
            scheduler_module.TreeScanScheduler,
            name,
            make(name, original),
        )

    # I6: within one scan a directory is dispatched exactly once. Counted per
    # scheduler, not globally -- a test that scans the same tree twice
    # legitimately dispatches the same path once per scan.
    dispatch_original = scheduler_module.TreeScanScheduler._next_child_job

    def counting_next_child_job(self, parent):
        job = dispatch_original(self, parent)
        if job is not None:
            dispatched = getattr(self, "_invariant_dispatched", None)
            if dispatched is None:
                dispatched = self._invariant_dispatched = Counter()
            dispatched[job.path] += 1
            if dispatched[job.path] > 1:
                raise SchedulerInvariantViolation(
                    f"[_next_child_job] {job.path} dispatched "
                    f"{dispatched[job.path]} times in one scan"
                )
        return job

    monkeypatch.setattr(
        scheduler_module.TreeScanScheduler,
        "_next_child_job",
        counting_next_child_job,
    )


def fingerprint(root) -> tuple:
    """Structure, aggregates, and child order of one tree, as a value.

    Taken when a live frame is published and again once the scan finishes: a
    frame that has already been drawn must compare equal, so any node filled
    in place afterwards shows up as a difference.
    """
    out = []
    stack = [root]
    while stack:
        node = stack.pop()
        out.append(
            (
                node.path,
                node.size,
                node.own_size,
                node.file_count,
                node.dir_count,
                tuple(child.path for child in node.children),
            )
        )
        stack.extend(child for child in node.children if child.is_dir)
    return tuple(sorted(out))


class PublicationTracker:
    """Whether copy-on-write still protects nodes that have been published."""

    def __init__(self):
        #: Dispatches whose node had already gone out in a frame. The state to
        #: reach; asserted non-zero so a test cannot pass by never getting here.
        self.late_dispatches = 0
        #: Those of them that copy-on-write would no longer protect.
        self.violations: list[str] = []


def track_publication_generations(monkeypatch) -> PublicationTracker:
    """Catch published nodes being stamped as current-generation.

    Copy-on-write only protects a node whose state generation is behind the
    scheduler's -- `_ensure_mutable` returns early otherwise and the node is
    edited in place. Whether a given run then *observably* rewrites a frame
    depends on thread timing, so this checks the stamping itself, which is
    deterministic.
    """
    tracker = PublicationTracker()
    published: dict[int, int] = {}
    publish_original = scheduler_module.TreeScanScheduler._publish_tree
    dispatch_original = scheduler_module.TreeScanScheduler._next_child_job

    def recording_publish_tree(self, *, force):
        before = self._generation
        result = publish_original(self, force=force)
        if self._generation == before:
            return result  # throttled; nothing actually went out
        stack = [self._states[self._root_path].node]
        while stack:
            node = stack.pop()
            published.setdefault(id(node), before)
            stack.extend(node.children)
        return result

    def checking_next_child_job(self, parent):
        job = dispatch_original(self, parent)
        if job is None:
            return job
        state = self._states[job.path]
        went_out_at = published.get(id(state.node))
        if went_out_at is not None:
            tracker.late_dispatches += 1
            if state.generation == self._generation:
                tracker.violations.append(
                    f"{job.path} went out in generation {went_out_at} but its "
                    f"state claims the current generation "
                    f"({state.generation}), so the next entry chunk edits the "
                    "published node in place"
                )
        return job

    monkeypatch.setattr(
        scheduler_module.TreeScanScheduler,
        "_publish_tree",
        recording_publish_tree,
    )
    monkeypatch.setattr(
        scheduler_module.TreeScanScheduler,
        "_next_child_job",
        checking_next_child_job,
    )
    return tracker


def count_risky_settles(monkeypatch) -> Counter:
    """Count settles that reach the state the cursor bug needed.

    ``risky`` counts a directory settling while its child cursor still has
    entries ahead of it *and* the settle-time sort moves a subdirectory to or
    past that cursor -- the exact shape that used to re-dispatch an already
    scanned directory. Fixture-shape tests assert this is non-zero so they
    cannot quietly stop reaching the interesting state.
    """
    counts: Counter = Counter()
    counted: set[int] = set()
    original = scheduler_module.TreeScanScheduler._mark_settled

    def counting_mark_settled(self, state):
        # Snapshot before the call: settling retires the cursor and sorts the
        # entries, so the risk has to be judged on the pre-call values. Which
        # directories a settle cascades through is left to the real walk
        # rather than re-derived here.
        before = [
            (candidate, candidate.next_child_index, list(candidate.node.children))
            for candidate in self._states.values()
            if id(candidate) not in counted
        ]
        result = original(self, state)
        for candidate, cursor, children in before:
            if not candidate.settled:
                continue
            counted.add(id(candidate))
            counts["settled"] += 1
            if cursor < len(children):
                counts["open_cursor"] += 1
                ordered = sorted(children, key=lambda c: (c.name, c.path))
                if any(child.is_dir for child in ordered[cursor:]):
                    counts["risky"] += 1
        return result

    monkeypatch.setattr(
        scheduler_module.TreeScanScheduler,
        "_mark_settled",
        counting_mark_settled,
    )
    return counts


def unretire_child_cursor(monkeypatch) -> None:
    """Reintroduce the historical defect: settle-time sort, cursor left stale.

    Restores each freshly settled directory's cursor to its pre-sort value
    instead of copying the production body, so this stays faithful even if
    ``_mark_settled`` is rewritten.
    """
    original = scheduler_module.TreeScanScheduler._mark_settled

    def broken_mark_settled(self, state):
        before = [
            (candidate, candidate.next_child_index)
            for candidate in self._states.values()
        ]
        result = original(self, state)
        for candidate, cursor in before:
            if candidate.settled:
                candidate.next_child_index = cursor
        return result

    monkeypatch.setattr(
        scheduler_module.TreeScanScheduler,
        "_mark_settled",
        broken_mark_settled,
    )


def validate_tree(root) -> list[str]:
    """Recompute every directory aggregate bottom-up; return any mismatches."""

    problems: list[str] = []
    order = []
    stack = [(root, False)]
    while stack:
        node, visited = stack.pop()
        if visited:
            order.append(node)
            continue
        stack.append((node, True))
        stack.extend((child, False) for child in node.children if child.is_dir)

    for node in order:
        directories = [child for child in node.children if child.is_dir]
        expected = (
            ("size", node.size, node.own_size + sum(c.size for c in directories)),
            (
                "file_count",
                node.file_count,
                sum(c.file_count for c in node.children),
            ),
            (
                "dir_count",
                node.dir_count,
                sum(1 + c.dir_count for c in directories),
            ),
        )
        for label, got, want in expected:
            if got != want:
                problems.append(f"{node.path} {label}: got {got}, expected {want}")
        seen: set[str] = set()
        for child in node.children:
            if child.path in seen:
                problems.append(f"{node.path} lists {child.path} twice")
            seen.add(child.path)
    return problems
