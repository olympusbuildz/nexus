"""OS3 replay: OS3-shaped requests, malformed model answers, no network.

Every case runs twice (repair off = what the bridge did before, repair on) and in both
response modes (JSON and SSE). `python -m tests.os3_replay` prints the same runs as a table.
"""

from __future__ import annotations

import logging

import pytest
from nexus import tool_repair

from tests.os3_cases import CASES, LAPTOP, STUDIO
from tests.os3_replay import Reply, call, load, replay, table, verdict

MODES = pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
EACH = pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])


def _run(case, *, repair, stream, headers):
    request = load(case.fixture)
    return request, replay(request, case.replies, repair=repair, stream=stream, headers=headers)


@EACH
@MODES
def test_before_is_plain_pass_through(case, stream, bridge_key, openrouter_key, auth_header):
    request, result = _run(case, repair=False, stream=stream, headers=auth_header)
    assert result.status == 200
    assert len(result.upstream.requests) == 1
    assert result.body == result.upstream.sent[0]
    judged = verdict(request, case.expect, result)
    assert (judged == "pass") == (case.kind == "control"), judged


@EACH
@MODES
def test_after_repair(case, stream, bridge_key, openrouter_key, auth_header):
    request, result = _run(case, repair=True, stream=stream, headers=auth_header)
    assert len(result.upstream.requests) == case.upstream_calls
    assert result.upstream.requests[0] == {**request, "stream": stream}
    judged = verdict(request, case.expect, result)
    if case.kind == "rejected":
        assert result.status == (200 if stream else 502)
        assert judged == "rejected (invalid_tool_call)"
        assert result.error["type"] == "upstream_error" and result.error["hint"]
        assert b'"tool_calls"' not in result.body
        if stream:
            assert result.body.endswith(b"data: [DONE]\n\n")
        return
    assert result.status == 200
    if case.kind == "fixed":
        assert judged == "pass"
        return
    assert result.body == result.upstream.sent[0], "nothing to repair, so bytes must not change"
    assert judged == "pass"


@MODES
def test_reask_is_one_plain_text_turn_on_the_same_request(
    stream, bridge_key, openrouter_key, auth_header
):
    case = next(c for c in CASES if c.id == "worker-missing-node-two-devices")
    request, result = _run(case, repair=True, stream=stream, headers=auth_header)
    first, second = result.upstream.requests
    assert second["stream"] is False
    assert "stream_options" not in second
    assert {k: v for k, v in second.items() if k not in {"messages", "stream"}} == {
        k: v for k, v in first.items() if k not in {"messages", "stream"}
    }
    assert second["messages"][: len(request["messages"])] == request["messages"]
    asked = second["messages"][len(request["messages"]) :]
    assert [m["role"] for m in asked] == ["assistant", "user"]
    assert "shell.node_id is required" in asked[1]["content"]
    assert STUDIO in asked[1]["content"] and LAPTOP in asked[1]["content"]
    assert "tool_calls" not in asked[0]


@MODES
def test_repair_failure_returns_the_upstream_answer(
    stream, monkeypatch, bridge_key, openrouter_key, auth_header
):
    async def boom(*args, **kwargs):
        raise RuntimeError("repair bug")

    monkeypatch.setattr(tool_repair, "_repair_message", boom)
    case = next(c for c in CASES if c.id == "worker-bad-escape")
    _, result = _run(case, repair=True, stream=stream, headers=auth_header)
    assert result.status == 200
    assert result.body == result.upstream.sent[0]


@MODES
def test_logs_name_fixes_never_content(stream, caplog, bridge_key, openrouter_key, auth_header):
    caplog.set_level(logging.DEBUG)
    for case_id in ("worker-bad-escape", "worker-missing-node-two-devices"):
        case = next(c for c in CASES if c.id == case_id)
        _run(case, repair=True, stream=stream, headers=auth_header)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "tool repair: fixes=" in logged
    for secret in ("Downloads", "dmg", "uptime", STUDIO, openrouter_key, bridge_key):
        assert secret not in logged


@MODES
def test_text_that_looks_like_a_tool_call_is_left_alone(
    stream, bridge_key, openrouter_key, auth_header
):
    """Known gap: a call written as prose is not salvaged. It must at least not change."""
    text = '{"name": "shell", "arguments": {"command": "uptime"}}'
    request = load("worker")
    result = replay(request, [Reply(content=text)], repair=True, stream=stream, headers=auth_header)
    assert result.status == 200
    assert result.body == result.upstream.sent[0]


@MODES
def test_requests_without_tools_are_untouched(stream, bridge_key, openrouter_key, auth_header):
    request = {k: v for k, v in load("worker").items() if k not in {"tools", "tool_choice"}}
    reply = Reply(calls=[call("shell", "{broken")])
    result = replay(request, [reply], repair=True, stream=stream, headers=auth_header)
    assert result.body == result.upstream.sent[0]
    assert len(result.upstream.requests) == 1


@MODES
def test_tool_choice_none_is_untouched(stream, bridge_key, openrouter_key, auth_header):
    request = {**load("worker"), "tool_choice": "none"}
    reply = Reply(calls=[call("bash", {"command": "ls"})])
    result = replay(request, [reply], repair=True, stream=stream, headers=auth_header)
    assert result.body == result.upstream.sent[0]
    assert len(result.upstream.requests) == 1


def test_table_covers_every_case(bridge_key, openrouter_key):
    rows = table().splitlines()
    assert len(rows) == len(CASES) + 2
    fixed = {c.fixture + c.summary for c in CASES if c.kind == "fixed"}
    for row in rows[2:]:
        cells = [c.strip() for c in row.strip("|").split("|")]
        if cells[0] + cells[1] in fixed:
            assert cells[2].startswith("FAIL") and cells[3] == "pass", row
