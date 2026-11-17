"""ToolTracker guard: a chat run cannot loop on the same tool or read without limit."""

from __future__ import annotations

from goldtrader.chat.agent import MAX_TOOL_CALLS, ToolTracker
from goldtrader.llm.provider import ToolSpec
from goldtrader.llm.runtime import Tool


def _tool(name: str, hits: list) -> Tool:
    def handler(inp):
        hits.append((name, inp))
        return {"data": name}

    return Tool(ToolSpec(name, "d", {"type": "object", "properties": {}}), handler)


def test_repeats_and_calls_past_the_limit_get_an_instruction_not_data():
    tracker, hits = ToolTracker(), []
    score = tracker.wrap(_tool("get_scorecard", hits))
    prepare = tracker.wrap(_tool("prepare_action", hits))
    tracker.reset(1)
    assert score.handler({"period": "today"}) == {"data": "get_scorecard"}
    assert score.handler({"period": "today"})["status"] == "duplicate_call"
    assert score.handler({"period": "week"}) == {"data": "get_scorecard"}  # new input is fine
    assert tracker.calls == ["get_scorecard", "get_scorecard"] and len(hits) == 2
    for i in range(MAX_TOOL_CALLS):
        score.handler({"period": f"p{i}"})
    assert len(tracker.calls) == MAX_TOOL_CALLS
    assert score.handler({"period": "another"})["status"] == "tool_limit_reached"
    assert prepare.handler({"type": "pause"}) == {"data": "prepare_action"}  # never blocked
    tracker.reset(1)  # the next question starts clean
    assert score.handler({"period": "today"}) == {"data": "get_scorecard"}
