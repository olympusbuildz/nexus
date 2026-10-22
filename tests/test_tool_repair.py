"""Unit tests for the pieces of tool_repair that the OS3 replay cannot reach directly."""

from __future__ import annotations

import asyncio
import json

import pytest
from nexus import tool_repair as tr

from tests.os3_cases import GARBLED_STUDIO, LAPTOP, STUDIO

NODES = {STUDIO: ["Studio Mini", "Studio-Mini.local", "Studio-Mini"], LAPTOP: ["Laptop"]}
SHELL = {"properties": {"node_id": {"type": "string"}, "command": {"type": "string"}}}


@pytest.mark.parametrize(
    "text",
    [
        '{"a": "line\\nbreak", "b": "tab\\t", "c": "quote\\"", "d": "back\\\\slash"}',
        '{"a": "caf\\u00e9", "b": "slash\\/"}',
    ],
)
def test_valid_json_is_never_rewritten(text):
    value, repaired = tr._parse_args(text)
    assert repaired is False
    assert value == json.loads(text)


def test_stray_backslashes_are_doubled_and_valid_escapes_kept():
    value, repaired = tr._parse_args('{"c": "find \\( -name a \\) \\n done \\u00e9 \\x"}')
    assert repaired is True
    assert value == {"c": "find \\( -name a \\) \n done é \\x"}


@pytest.mark.parametrize("raw", ["{broken", "[1, 2", "not json at all", 5])
def test_unparseable_arguments_are_left_alone(raw):
    value, repaired = tr._parse_args(raw)
    assert value is tr._BAD and repaired is False


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Studio Mini", STUDIO),
        ("studio mini", STUDIO),
        ("STUDIO-MINI.LOCAL", STUDIO),
        ("laptop", LAPTOP),
        (GARBLED_STUDIO, STUDIO),
        (STUDIO.upper(), STUDIO),
        (None, None),
        ("", ""),
        ("a different machine", "a different machine"),
        ("0000-far-off", "0000-far-off"),
    ],
)
def test_node_id_is_resolved_only_when_unambiguous(given, expected):
    args = {"command": "ls"} if given is None else {"command": "ls", "node_id": given}
    tr._fix_node_id(args, SHELL, NODES)
    assert args.get("node_id") == expected


def test_a_single_device_fills_a_missing_node_id():
    args = {"command": "ls"}
    assert tr._fix_node_id(args, SHELL, {STUDIO: ["Studio Mini"]})
    assert args["node_id"] == STUDIO


def test_node_id_is_left_alone_when_the_tool_has_none():
    args = {"command": "ls", "node_id": "Studio Mini"}
    assert not tr._fix_node_id(args, {"properties": {"command": {}}}, NODES)
    assert args["node_id"] == "Studio Mini"


def test_nodes_come_from_the_prompt_markup():
    text = (
        f'<nodes><node id="{STUDIO}" name="Studio Mini" default="true">'
        "<hostname>Studio-Mini.local</hostname></node></nodes>"
    )
    assert tr._parse_nodes(text) == {STUDIO: ["Studio Mini", "Studio-Mini.local", "Studio-Mini"]}
    assert tr._parse_nodes("no devices here") == {}


@pytest.mark.parametrize(
    ("value", "schema", "bad"),
    [
        (5, {"type": "integer"}, False),
        (5.0, {"type": "integer"}, False),
        (5.5, {"type": "integer"}, True),
        (True, {"type": "integer"}, True),
        ("5", {"type": "integer"}, True),
        (None, {"type": ["string", "null"]}, False),
        ("a", {"enum": ["a", "b"]}, False),
        ("c", {"enum": ["a", "b"]}, True),
        ({}, {"type": "object", "required": ["x"]}, True),
        ({"x": 1}, {"type": "object", "required": ["x"]}, False),
        (["a", 1], {"type": "array", "items": {"type": "string"}}, True),
        (1, {"oneOf": [{"type": "string"}]}, False),
        (1, "not a schema", False),
    ],
)
def test_schema_subset(value, schema, bad):
    assert bool(tr._schema_problems(value, schema, "t")) is bad


def test_unknown_script_names_become_act_py_actions():
    args = {"script": "wait", "args": ["--duration", "1"]}
    assert tr._fix_computer_use(args)
    assert args == {"script": "act.py", "args": ["wait", "--duration", "1"]}
    path_args = {"script": "~/.os3/computer-use/capture.py"}
    assert tr._fix_computer_use(path_args)
    assert path_args["script"] == "capture.py"
    fine = {"script": "act.py", "args": ["wait"]}
    assert not tr._fix_computer_use(fine)


async def _events(chunks):
    for chunk in chunks:
        yield chunk


def _guard(chunks, tools=None):
    ctx = tr._Ctx(tools=tools or {"shell": SHELL}, nodes=NODES)

    async def no_reask(calls, problems):
        return None

    async def collect():
        return [e async for e in tr._guard_stream(_events(chunks), ctx, no_reask)]

    return asyncio.run(collect())


def _data(obj):
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _chunk(delta, finish=None):
    return _data(
        {
            "id": "x",
            "model": "m",
            "created": 1,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
    )


def test_stream_text_is_forwarded_as_it_arrives_and_tool_calls_are_held():
    call = {
        "index": 0,
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "shell",
            "arguments": json.dumps({"command": "ls", "node_id": STUDIO}),
        },
    }
    text, start, end = (
        _chunk({"content": "ok"}),
        _chunk({"tool_calls": [call]}),
        b"data: [DONE]\n\n",
    )
    seen: list[str] = []

    async def source():
        yield text
        seen.append("after text")
        yield start
        yield end

    ctx = tr._Ctx(tools={"shell": SHELL}, nodes=NODES)

    async def no_reask(calls, problems):
        return None

    async def run():
        out = []
        async for event in tr._guard_stream(source(), ctx, no_reask):
            out.append((event, list(seen)))
        return out

    out = asyncio.run(run())
    assert out[0] == (text, [])
    assert [e for e, _ in out[1:]] == [start, end]


def test_stream_with_crlf_events_and_no_index_is_reassembled():
    first = {
        "id": "call_1",
        "function": {"name": "shell", "arguments": '{"command": "ls \\(", "node_id": "Laptop"}'},
    }
    second = {
        "id": "call_2",
        "function": {"name": "shell", "arguments": '{"command": "pwd", "node_id": "laptop"}'},
    }
    raw = (
        b"".join(_chunk({"tool_calls": [t]}).replace(b"\n\n", b"\r\n\r\n") for t in (first, second))
        + b"data: [DONE]\r\n\r\n"
    )
    events = _guard([raw[:25], raw[25:]])
    body = b"".join(events)
    assert body.endswith(b"data: [DONE]\n\n")
    calls = json.loads(body.split(b"\n\n")[0][len(b"data: ") :])["choices"][0]["delta"][
        "tool_calls"
    ]
    assert [c["index"] for c in calls] == [0, 1]
    assert json.loads(calls[1]["function"]["arguments"]) == {"command": "pwd", "node_id": LAPTOP}
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls \\(", "node_id": LAPTOP}


def test_usage_chunk_survives_a_rewrite():
    start = _chunk(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "c",
                    "function": {
                        "name": "shell",
                        "arguments": json.dumps({"command": "ls", "node_id": "Laptop"}),
                    },
                }
            ]
        }
    )
    usage = _data({"id": "x", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    events = _guard([start, usage, b"data: [DONE]\n\n"])
    parsed = [json.loads(e[len(b"data: ") :]) for e in events[:-1]]
    assert parsed[-1]["usage"] == {"prompt_tokens": 3, "completion_tokens": 1}
    assert parsed[-1]["choices"] == []
