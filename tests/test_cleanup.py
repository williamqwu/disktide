"""Tests for cleanup detection."""

from disktide.models.tree import FSNode
from disktide.cleanup.detector import detect_targets, total_savings


def make_project_tree():
    """Create a tree simulating a project with cleanable targets."""
    root = FSNode(
        name="project", path="/project", size=10000, own_size=100,
        is_dir=True, depth=0, file_count=50, dir_count=5,
    )

    # package.json (indicator for node_modules)
    pkg_json = FSNode(
        name="package.json", path="/project/package.json", size=500,
        own_size=500, is_dir=False, depth=1, file_count=1,
    )

    # node_modules
    node_modules = FSNode(
        name="node_modules", path="/project/node_modules", size=5000,
        own_size=5000, is_dir=True, depth=1, file_count=30, dir_count=10,
    )

    # __pycache__
    pycache = FSNode(
        name="__pycache__", path="/project/__pycache__", size=200,
        own_size=200, is_dir=True, depth=1, file_count=5,
    )

    # .DS_Store
    ds_store = FSNode(
        name=".DS_Store", path="/project/.DS_Store", size=8,
        own_size=8, is_dir=False, depth=1, file_count=1,
    )

    # Regular source file (should NOT be matched)
    src = FSNode(
        name="main.py", path="/project/main.py", size=1000,
        own_size=1000, is_dir=False, depth=1, file_count=1,
    )

    root.children = [pkg_json, node_modules, pycache, ds_store, src]
    return root


class TestDetector:
    def test_detect_node_modules(self):
        root = make_project_tree()
        # Use a custom rule without parent_indicators for unit testing
        # (the real rule requires package.json on the actual filesystem)
        from disktide.models.patterns import CleanupRule, RiskLevel
        rules = [
            CleanupRule(
                name="node_modules", description="test",
                patterns=["node_modules"], risk=RiskLevel.SAFE,
                parent_indicators=[],  # skip fs check in unit test
                category="dependencies",
            ),
        ]
        targets = detect_targets(root, rules=rules)
        paths = {t.path for t in targets}
        assert "/project/node_modules" in paths

    def test_detect_pycache(self):
        root = make_project_tree()
        targets = detect_targets(root)
        paths = {t.path for t in targets}
        assert "/project/__pycache__" in paths

    def test_detect_ds_store(self):
        root = make_project_tree()
        targets = detect_targets(root)
        paths = {t.path for t in targets}
        assert "/project/.DS_Store" in paths

    def test_no_false_positives(self):
        root = make_project_tree()
        targets = detect_targets(root)
        paths = {t.path for t in targets}
        assert "/project/main.py" not in paths
        assert "/project/package.json" not in paths

    def test_total_savings(self):
        root = make_project_tree()
        targets = detect_targets(root)
        total = total_savings(targets)
        assert total > 0
