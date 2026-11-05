"""Static import isolation (AGENTS §1 "least privilege", R-010, R-034): no bot module and not the
decision cycle may import MT5, the execution engine or the gateway state builder, directly (AST)
or transitively (import closure in a fresh interpreter). Also: the permission check refuses an
unlisted tool, and the registry's defaults match AGENTS §3/§4."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from goldtrader.bots.registry import DEFAULT_PERMISSIONS, BotRegistry, check_permissions
from goldtrader.llm.provider import ToolSpec
from goldtrader.llm.runtime import Tool
from tests.test_bots_helpers import cfg, db

SRC = Path(__file__).resolve().parents[1] / "src" / "goldtrader"
WATCHED = sorted(SRC.glob("bots/*.py")) + [SRC / "brain" / "cycle.py"]
FORBIDDEN = ("goldtrader.mt5", "goldtrader.execution", "goldtrader.gateway.state")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_direct_forbidden_imports():
    assert WATCHED, "no bot modules found"
    for path in WATCHED:
        bad = [m for m in _imports(path) if m.startswith(FORBIDDEN)]
        assert not bad, f"{path.name} imports {bad}"


def test_no_transitive_forbidden_imports():
    code = (
        "import json, sys\n"
        "import goldtrader.bots, goldtrader.brain.cycle\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('goldtrader'))))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(SRC.parent),
    )
    loaded = json.loads(out.stdout.strip().splitlines()[-1])
    bad = [m for m in loaded if m.startswith(FORBIDDEN)]
    assert not bad, f"transitively imported: {bad}"
    assert "goldtrader.brain.gates" in loaded  # the cycle really uses the shared gates


def test_default_permissions_only_research_has_tools():
    conn, store = db()
    reg = BotRegistry(cfg(), None, store, conn)
    for name, spec in reg.specs.items():
        tools = {t.spec.name for t in spec.tools}
        assert tools <= set(DEFAULT_PERMISSIONS[name]), name
        if name not in ("research", "weekly_research"):
            assert tools == set(), f"{name} must have no tools (AGENTS §3.2–3.4)"


def test_permissions_check_fails_for_unlisted_tool():
    conn, store = db()
    reg = BotRegistry(cfg(), None, store, conn)
    rogue = Tool(ToolSpec("place_order", "nope", {"type": "object", "properties": {}}), lambda i: 0)
    specs = dict(reg.specs)
    specs["strategy"] = specs["strategy"].__class__(
        **{**specs["strategy"].__dict__, "tools": [rogue]}
    )
    with pytest.raises(PermissionError, match="strategy:place_order"):
        check_permissions(specs, {})
    # an explicit owner mapping replaces the defaults: research without search_memory fails
    with pytest.raises(PermissionError, match="research:search_memory"):
        check_permissions(reg.specs, {"research": ("get_features",)})
    # and a mapping that grants it passes (other bots have no tools, so they need no entry)
    check_permissions(
        reg.specs,
        {
            "research": ("search_memory", "get_features"),
            "weekly_research": ("search_memory", "get_features"),
        },
    )
    with pytest.raises(PermissionError):
        BotRegistry(
            cfg(owner={"risk": {"max_volume_lots": 1.0}, "bot_permissions": {"research": []}}),
            None,
            store,
            conn,
        )
