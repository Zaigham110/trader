"""R-034 / D-045 enforcement: the runtime guard raises when the owner section changes, and the
learning package has no static or transitive path to a config writer, yaml.dump, the gateway, MT5
or the execution engine (LEARNING_SYSTEM §2 "a test attempts the write and must fail")."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from goldtrader.config.loader import owner_hash, parse_config
from goldtrader.learning.immutable import OwnerGuard, assert_owner_untouched, guard

SRC = Path(__file__).resolve().parents[1] / "src" / "goldtrader"
LEARNING = sorted((SRC / "learning").glob("*.py"))
FORBIDDEN_MODULES = (  # no learning module may import these directly
    "goldtrader.mt5",
    "goldtrader.execution",
    "goldtrader.gateway",
    "goldtrader.config.loader.write",
    "yaml",
)
BROKER_IO = (  # and nothing may reach them transitively (the backtester's session clock imports
    "goldtrader.mt5.real",  # only `goldtrader.mt5.constants`, which has no I/O)
    "goldtrader.mt5.onaccount",
    "goldtrader.execution",
    "goldtrader.gateway",
)
FORBIDDEN_NAMES = {"write_config", "save_config", "dump", "safe_dump"}


def _cfg(**owner):
    return parse_config({"owner": {"risk": {"max_volume_lots": 0.05, **owner}}})


def test_assert_owner_untouched():
    assert_owner_untouched("abc", "abc")
    with pytest.raises(PermissionError, match="owner section changed"):
        assert_owner_untouched("abc", "abd")


def test_owner_guard_pins_hash():
    cfg = _cfg()
    g = OwnerGuard(cfg)
    assert g.check(cfg) == owner_hash(cfg)
    assert g.check(parse_config({"owner": {"risk": {"max_volume_lots": 0.05}}, "version": 2}))
    with pytest.raises(PermissionError):
        g.check(_cfg(daily_loss_pct=1.0))


def test_guard_raises_when_a_call_changes_the_owner_section():
    class Thing:
        def __init__(self, cfg):
            self.cfg = cfg
            self._owner_guard = OwnerGuard(cfg)

        @guard
        def fine(self, x):
            return x + 1

        @guard
        def rogue(self):
            self.cfg = _cfg(daily_loss_pct=1.0)  # the only way to "write": swap the object
            return "done"

    t = Thing(_cfg())
    assert t.fine(1) == 2
    with pytest.raises(PermissionError):
        t.rogue()
    # and a pinned guard refuses a config whose owner section differs from construction time
    t2 = Thing(_cfg())
    t2.cfg = _cfg(daily_loss_pct=1.0)
    with pytest.raises(PermissionError):
        t2.fine(1)


def _imports(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    attrs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Attribute):
            attrs.add(node.attr)
        elif isinstance(node, ast.Name):
            attrs.add(node.id)
    return modules, attrs


def test_learning_package_has_no_static_write_or_execution_path():
    assert len(LEARNING) >= 8
    for path in LEARNING:
        modules, names = _imports(path)
        bad = [m for m in modules if m.startswith(FORBIDDEN_MODULES)]
        assert not bad, f"{path.name} imports {bad}"
        assert not (names & FORBIDDEN_NAMES), f"{path.name} uses {names & FORBIDDEN_NAMES}"
        text = path.read_text(encoding="utf-8")
        assert "UPDATE config" not in text and "INSERT INTO config_versions" not in text


def test_learning_package_has_no_transitive_forbidden_imports():
    code = (
        "import json, sys\n"
        "import goldtrader.learning, goldtrader.learning.pipeline\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('goldtrader') "
        "or m == 'yaml')))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(SRC.parent),
    )
    loaded = json.loads(out.stdout.strip().splitlines()[-1])
    bad = [m for m in loaded if m.startswith(BROKER_IO)]
    assert not bad, f"transitively imported: {bad}"
    assert "goldtrader.learning.pipeline" in loaded
    assert "goldtrader.mt5.real" not in loaded  # the only module that imports MetaTrader5
