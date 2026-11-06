from __future__ import annotations

from pathlib import Path

import pytest

from repofix.context import ContextManager
from repofix.models import Event, TaskSpec
from repofix.policy import PolicyEngine, PolicyViolation
from repofix.runtime import AgentHarness


def test_policy_confines_paths_and_forbidden_files(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ok.py").write_text("x = 1", encoding="utf-8")
    policy = PolicyEngine(workspace, TaskSpec(issue="fix"))
    assert policy.resolve_path("ok.py", must_exist=True) == workspace / "ok.py"
    with pytest.raises(PolicyViolation, match="escapes"):
        policy.resolve_path("../outside.txt")
    with pytest.raises(PolicyViolation, match="denied"):
        policy.resolve_path(".git/config")


def test_context_compacts_but_keeps_goal_and_recent_messages():
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "goal"},
        *({"role": "tool", "content": "x" * 700, "tool_call_id": str(i)} for i in range(20)),
        {"role": "assistant", "content": "recent"},
    ]
    events = [
        Event(
            seq=1,
            session_id="s",
            kind="tool_finished",
            payload={"name": "pytest", "status": "error", "summary": "one failure"},
        )
    ]
    compacted = ContextManager(4000).compact(messages, events)
    assert compacted[0]["content"] == "rules"
    assert compacted[1]["content"] == "goal"
    assert any("one failure" in message.get("content", "") for message in compacted)
    assert compacted[-1]["content"] == "recent"
    assert len(str(compacted)) < len(str(messages))


def test_compaction_keeps_tool_request_and_result_together():
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "run_command", "arguments": '{"command":"pytest"}'},
            }
        ],
    }
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "goal"},
        assistant,
        {"role": "tool", "tool_call_id": "call-1", "content": "x" * 10_000},
    ]
    compacted = ContextManager(4000).compact(messages, [])
    tool_index = next(i for i, item in enumerate(compacted) if item.get("role") == "tool")
    assert compacted[tool_index - 1].get("role") == "assistant"
    assert compacted[tool_index - 1]["tool_calls"][0]["id"] == "call-1"
    assert len(compacted[tool_index]["content"]) < 4000


def test_recovery_repairs_missing_tool_results_without_replaying_calls():
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "goal"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "started",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": "{}"},
                },
                {
                    "id": "not-started",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
            ],
        },
        {"role": "user", "content": "recovery notice"},
    ]

    repaired, inserted = AgentHarness._repair_missing_tool_messages(messages, {"started"})

    assistant_index = next(i for i, item in enumerate(repaired) if item["role"] == "assistant")
    tool_messages = repaired[assistant_index + 1 : assistant_index + 3]
    assert inserted == ["started", "not-started"]
    assert [item["role"] for item in tool_messages] == ["tool", "tool"]
    assert "indeterminate" in tool_messages[0]["content"]
    assert "cancelled" in tool_messages[1]["content"]
