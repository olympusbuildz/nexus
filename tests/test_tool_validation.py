"""Tool-call validation and repair: one test per repair and per rejection, then the same
answers through the HTTP server (OpenAI chat, Anthropic Messages on a chat backend, and
Anthropic Messages on a native backend), streaming and not.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from nexus import tool_repair as tr
from nexus.anthropic_messages import sse_event
from nexus.app import create_app
from nexus.backends.anthropic import AnthropicBackend
from nexus.backends.openrouter import OpenRouterBackend
from nexus.errors import GatewayError
from nexus.pulse import tool_repair_counts

from tests.os3_replay import FakeUpstream, Reply, call, error_of, read_stream
from tests.transport import HandlerTransport

VOLUME = {
    "type": "object",
    "properties": {
        "level": {"type": "integer"},
        "gain": {"type": "number"},
        "room": {"type": "string"},
        "mode": {"type": "string", "enum": ["quiet", "loud"]},
        "tags": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["level"],
    "additionalProperties": False,
}
MUSIC = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}
TOOLS = {"set_volume": VOLUME, "play_music": MUSIC, "play_movie": MUSIC}


def _ctx(tools=None) -> tr._Ctx:
    return tr._Ctx(tools=tools or TOOLS, nodes={})


def _raw(name, args, id="call_1"):
    arguments = args if isinstance(args, str) else json.dumps(args)
    return {"id": id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _fixed_args(args):
    checked = tr._check([_raw("set_volume", args)], _ctx())
    assert checked.problems == [], checked.problems
    return json.loads(checked.calls[0]["function"]["arguments"]), checked.fixes


@pytest.mark.parametrize(
    "text",
    [
        '{"level": 3,}',
        '{"level": 3 , }',
        '{"tags": [1, 2,], "level": 3}',
        '{"level": 3,\n}',
    ],
)
def test_trailing_comma_is_dropped(text):
    value, repaired = tr._parse_args(text)
    assert repaired is True
    assert value["level"] == 3


def test_a_comma_inside_a_string_is_not_a_trailing_comma():
    value, _ = tr._parse_args('{"room": "a,}", "level": "x\\",]",}')
    assert value == {"room": "a,}", "level": 'x",]'}


@pytest.mark.parametrize("junk", ["}", "\n</tool_call>", " Done.", "```"])
def test_trailing_junk_after_the_object_is_dropped(junk):
    value, repaired = tr._parse_args('{"level": 3}' + junk)
    assert (value, repaired) == ({"level": 3}, True)


@pytest.mark.parametrize(
    "text",
    [
        '{"level": 3}{"level": 4}',
        '{"level": 3} [1]',
        'Sure: {"level": 3}',
        '{"level": 3',
        '[{"level": 3}] x',
    ],
)
def test_arguments_that_would_lose_data_are_not_guessed_at(text):
    value, repaired = tr._parse_args(text)
    assert value is tr._BAD and repaired is False


def test_fenced_and_double_encoded_arguments_still_parse():
    assert tr._parse_args('```json\n{"level": 3}\n```') == ({"level": 3}, True)
    assert tr._parse_args(json.dumps(json.dumps({"level": 3}))) == ({"level": 3}, True)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("SET_VOLUME", "set_volume"),
        (" Set_Volume ", "set_volume"),
        ("set_vol", None),
        ("play_mu", None),
        ("play_", None),
        ("play", None),
        ("volume", None),
        ("set_volume_extra", None),
        ("", None),
        ("   ", None),
    ],
)
def test_tool_name_resolves_only_by_case_and_spaces_never_by_prefix(given, expected):
    assert tr._resolve_name(given, TOOLS) == expected


def test_case_insensitive_names_that_collide_are_not_resolved():
    tools = {"Run": {}, "run": {}, "walk": {}}
    assert tr._resolve_name("RUN ", tools) is None
    assert tr._resolve_name("WALK", tools) == "walk"


def test_a_resolved_name_is_written_back_and_logged_as_a_fix():
    checked = tr._check([_raw("Set_Volume", {"level": 1})], _ctx())
    assert checked.problems == []
    assert checked.calls[0]["function"]["name"] == "set_volume"
    assert "tool_name" in checked.fixes


def test_an_unknown_tool_is_a_problem_and_its_name_is_made_safe():
    checked = tr._check([_raw("rm -rf /\n" + "x" * 200, {})], _ctx())
    [problem] = checked.problems
    assert "\n" not in problem and "/" not in problem.split(";")[0]
    assert len(problem.split(";")[0]) < 120


def test_numeric_strings_become_numbers_where_the_schema_says_so():
    args, fixes = _fixed_args({"level": "7", "gain": "1.5", "tags": ["1", "2"]})
    assert args == {"level": 7, "gain": 1.5, "tags": [1, 2]}
    assert "coerce" in fixes


def test_a_whole_number_string_for_a_number_field_becomes_an_int():
    args, _ = _fixed_args({"level": 1, "gain": "2"})
    assert args["gain"] == 2 and isinstance(args["gain"], int)


@pytest.mark.parametrize(
    ("schema", "value"),
    [
        ({"type": "string"}, "7"),
        ({"type": ["string", "integer"]}, "7"),
        ({"type": "integer", "enum": ["7"]}, "7"),
        ({"type": "integer"}, "7.5"),
        ({"type": "integer"}, "seven"),
        ({"type": "integer"}, ""),
        ({"type": "integer"}, "1e3"),
        ({"type": "number"}, "1e999"),
        ({"type": "boolean"}, "true"),
        ({}, "7"),
    ],
)
def test_strings_are_left_alone_unless_the_schema_clearly_wants_a_number(schema, value):
    fixes: set[str] = set()
    assert tr._normalize(value, schema, fixes) == value
    assert fixes == set()


def test_unknown_keys_are_dropped_only_when_additional_properties_is_false():
    args, fixes = _fixed_args({"level": 1, "volume": 11})
    assert args == {"level": 1} and "extra_args" in fixes
    checked = tr._check([_raw("play_music", {"title": "x", "volume": 11})], _ctx())
    assert json.loads(checked.calls[0]["function"]["arguments"]) == {"title": "x", "volume": 11}
    assert checked.fixes == []


def test_unknown_keys_are_kept_when_pattern_properties_could_allow_them():
    schema = {"additionalProperties": False, "patternProperties": {"^x_": {}}, "properties": {}}
    value = {"x_a": 1}
    tr._normalize(value, schema, set())
    assert value == {"x_a": 1}


def test_nested_objects_are_normalized_too():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"n": {"type": "integer"}},
                },
            }
        },
    }
    value = {"items": [{"n": "4", "extra": 1}]}
    tr._normalize(value, schema, set())
    assert value == {"items": [{"n": 4}]}


def test_a_missing_required_field_is_reported_never_invented():
    checked = tr._check([_raw("set_volume", {"room": "den"})], _ctx())
    assert checked.problems == ["set_volume.level is required"]
    assert json.loads(checked.calls[0]["function"]["arguments"]) == {"room": "den"}


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        ({"level": "loud"}, "set_volume.level must be integer"),
        ({"level": 1.5}, "set_volume.level must be integer"),
        ({"level": True}, "set_volume.level must be integer"),
        ({"level": 1, "room": 5}, "set_volume.room must be string"),
        ({"level": 1, "mode": "medium"}, "set_volume.mode must be one of ['quiet', 'loud']"),
        ({"level": 1, "tags": [1, "x"]}, "set_volume.tags[1] must be integer"),
        ({"level": 1, "tags": "1"}, "set_volume.tags must be array"),
    ],
)
def test_type_and_enum_problems_are_reported(args, problem):
    checked = tr._check([_raw("set_volume", args)], _ctx())
    assert checked.problems == [problem]


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ("{broken", "set_volume: arguments are not valid JSON"),
        ("[1, 2]", "set_volume: arguments must be a JSON object"),
        ("7", "set_volume: arguments must be a JSON object"),
        (7, "set_volume: arguments are not valid JSON"),
    ],
)
def test_arguments_that_are_not_an_object_are_reported(arguments, problem):
    raw = _raw("set_volume", "")
    raw["function"]["arguments"] = arguments
    assert tr._check([raw], _ctx()).problems == [problem]


def test_every_call_in_a_turn_is_checked():
    calls = [
        _raw("set_volume", {"level": 1}, "a"),
        _raw("play_music", {}, "b"),
        _raw("nope", {}, "c"),
        {"id": "d", "type": "function"},
    ]
    problems = tr._check(calls, _ctx()).problems
    assert len(problems) == 3
    assert any("play_music.title" in p for p in problems)
    assert any("no tool named 'nope'" in p for p in problems)
    assert any("no function object" in p for p in problems)


def test_a_valid_call_is_returned_untouched():
    raw = _raw("set_volume", '{"level": 3,  "room":"den"}')
    checked = tr._check([raw], _ctx())
    assert checked.problems == [] and checked.fixes == [] and checked.advice == []
    assert checked.calls == [raw]


def test_a_device_id_the_prompt_does_not_list_is_advice_not_a_rejection():
    ctx = tr._Ctx(
        tools={"shell": {"properties": {"node_id": {"type": "string"}}}}, nodes={"n1": []}
    )
    checked = tr._check([_raw("shell", {"node_id": "zzzzzzzzzzzzzz"})], ctx)
    assert checked.problems == []
    assert checked.advice == ["shell.node_id must be the id of a registered device"]


def test_the_rejection_is_a_client_safe_invalid_tool_call_error():
    problems = [f"set_volume.f{i} is required" for i in range(8)]
    with pytest.raises(GatewayError) as caught:
        tr._reject(problems, ["args"])
    error = caught.value.as_dict()["error"]
    assert caught.value.status_code == 502
    assert error["code"] == "invalid_tool_call"
    assert error["type"] == "upstream_error"
    assert "set_volume.f4 is required" in error["message"]
    assert "f5" not in error["message"] and "and 3 more" in error["message"]
    assert "NEXUS_TOOL_REPAIR" in error["hint"]


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "OFF"])
def test_the_switch_turns_repair_off(monkeypatch, value):
    payload = {"tools": [{"type": "function", "function": {"name": "a", "parameters": {}}}]}
    assert tr._context(payload) is not None
    monkeypatch.setenv("OS3_BRIDGE_TOOL_REPAIR", value)
    assert tr._context(payload) is None


MODEL = "meituan/longcat-2.5-preview:free"
FN_TOOLS = [
    {"type": "function", "function": {"name": n, "parameters": s}} for n, s in TOOLS.items()
]
CHAT = {"model": MODEL, "messages": [{"role": "user", "content": "go"}], "tools": FN_TOOLS}
MODES = pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])


def _post(path, body, replies, headers):
    upstream = FakeUpstream(replies)
    app = create_app(backend=OpenRouterBackend(transport=upstream.transport))
    with TestClient(app) as client:
        return client.post(path, json=body, headers=headers), upstream


def _counts():
    return tool_repair_counts()


@MODES
def test_chat_every_broken_call_in_one_turn_is_repaired(
    stream, auth_header, openrouter_key, caplog
):
    caplog.set_level(logging.INFO)
    before = _counts()
    replies = [
        Reply(
            calls=[
                call("Set_Volume", '{"level": "7", "gain": "0.5", "volume": 11,}', "c1"),
                call("play_music", {"title": "Ode"}, "c2"),
                call("PLAY_MUSIC ", '```json\n{"title": "Ode"}\n```', "c3"),
                call("play_music", '{"title": "Ode"}\n</tool_call>', "c4"),
            ]
        )
    ]
    response, upstream = _post(
        "/v1/chat/completions", {**CHAT, "stream": stream}, replies, auth_header
    )
    assert response.status_code == 200 and len(upstream.requests) == 1
    message = read_stream(response.content) if stream else response.json()["choices"][0]["message"]
    got = [
        (c["id"], c["function"]["name"], json.loads(c["function"]["arguments"]))
        for c in message["tool_calls"]
    ]
    assert got == [
        ("c1", "set_volume", {"level": 7, "gain": 0.5}),
        ("c2", "play_music", {"title": "Ode"}),
        ("c3", "play_music", {"title": "Ode"}),
        ("c4", "play_music", {"title": "Ode"}),
    ]
    if not stream:
        assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert _counts()["repaired"] == before["repaired"] + 1
    assert _counts()["rejected"] == before["rejected"]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("tool repair")]
    assert len(lines) == 1
    assert "coerce" in lines[0] and "extra_args" in lines[0] and "tool_name" in lines[0]
    assert "Ode" not in lines[0] and openrouter_key not in lines[0]


@MODES
def test_chat_a_valid_call_goes_out_byte_for_byte(stream, auth_header, openrouter_key):
    before = _counts()
    reply = Reply(calls=[call("set_volume", {"level": 3, "room": "den"}, "c1")])
    response, upstream = _post(
        "/v1/chat/completions", {**CHAT, "stream": stream}, [reply], auth_header
    )
    assert response.content == upstream.sent[0]
    assert _counts() == before


@MODES
def test_chat_an_unrepairable_call_is_a_clean_invalid_tool_call_error(
    stream, auth_header, openrouter_key, caplog
):
    caplog.set_level(logging.INFO)
    before = _counts()
    secret_arg = "do-not-log-this-argument"
    bad = call("set_volume", {"room": secret_arg}, "c1")
    replies = [Reply(calls=[call("play_music", {"title": "Ode"}, "c0"), bad])] * 2
    response, upstream = _post(
        "/v1/chat/completions", {**CHAT, "stream": stream}, replies, auth_header
    )
    assert len(upstream.requests) == 2, "one re-ask, then give up"
    assert response.status_code == (200 if stream else 502)
    error = error_of(response.content)
    assert error["code"] == "invalid_tool_call" and error["type"] == "upstream_error"
    assert "set_volume.level is required" in error["message"]
    assert error["hint"]
    assert b'"tool_calls"' not in response.content
    assert secret_arg not in response.text and secret_arg not in error["message"]
    if stream:
        assert response.content.endswith(b"data: [DONE]\n\n")
    assert _counts()["rejected"] == before["rejected"] + 1
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "rejected code=invalid_tool_call" in logged
    assert secret_arg not in logged and openrouter_key not in logged


@MODES
def test_chat_a_failed_reask_after_text_still_ends_the_stream_cleanly(
    stream, auth_header, openrouter_key
):
    replies = [Reply(content="Working on it. ", calls=[call("nope", {})]), Reply(status=500)]
    response, _ = _post("/v1/chat/completions", {**CHAT, "stream": stream}, replies, auth_header)
    assert error_of(response.content)["code"] == "invalid_tool_call"
    if stream:
        assert b"Worki" in response.content
        assert response.content.count(b"data: [DONE]") == 1


@MODES
def test_chat_the_switch_restores_pass_through(stream, monkeypatch, auth_header, openrouter_key):
    monkeypatch.setenv("OS3_BRIDGE_TOOL_REPAIR", "off")
    reply = Reply(calls=[call("Set_Volume", "{broken", "c1")])
    response, upstream = _post(
        "/v1/chat/completions", {**CHAT, "stream": stream}, [reply], auth_header
    )
    assert response.status_code == 200 and len(upstream.requests) == 1
    assert response.content == upstream.sent[0]


def test_the_counters_are_on_the_pulse_endpoint(auth_header, openrouter_key):
    app = create_app(backend=OpenRouterBackend(transport=FakeUpstream([]).transport))
    with TestClient(app) as client:
        counts = client.get("/dashboard/pulse").json()["traffic"]["tool_repair"]
    assert set(counts) == {"repaired", "rejected"}


MESSAGES = {
    "model": MODEL,
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "go"}],
    "tools": [{"name": n, "input_schema": s} for n, s in TOOLS.items()],
}


def _tool_uses(response, stream):
    if not stream:
        return [b for b in response.json()["content"] if b["type"] == "tool_use"]
    blocks: dict[int, dict] = {}
    for event in response.text.split("\n\n"):
        data = [line[6:] for line in event.splitlines() if line.startswith("data: ")]
        obj = json.loads(data[0]) if data else {}
        if obj.get("type") == "content_block_start" and obj["content_block"]["type"] == "tool_use":
            blocks[obj["index"]] = {**obj["content_block"], "json": ""}
        if obj.get("type") == "content_block_delta" and obj["delta"]["type"] == "input_json_delta":
            blocks[obj["index"]]["json"] += obj["delta"]["partial_json"]
    return [
        {"id": b["id"], "name": b["name"], "input": json.loads(b["json"] or "{}")}
        for _, b in sorted(blocks.items())
    ]


@MODES
def test_messages_on_a_chat_backend_repairs_tool_use(stream, auth_header, openrouter_key):
    replies = [
        Reply(
            calls=[
                call("SET_VOLUME", '{"level": "7",}', "c1"),
                call("play_music", {"title": "Ode"}, "c2"),
            ]
        )
    ]
    response, upstream = _post("/v1/messages", {**MESSAGES, "stream": stream}, replies, auth_header)
    assert response.status_code == 200 and len(upstream.requests) == 1
    got = [(b["name"], b["input"]) for b in _tool_uses(response, stream)]
    assert got == [("set_volume", {"level": 7}), ("play_music", {"title": "Ode"})]


@MODES
def test_messages_on_a_chat_backend_rejects_what_it_cannot_repair(
    stream, auth_header, openrouter_key
):
    replies = [Reply(calls=[call("set_volume", {"room": "den"}, "c1")])] * 2
    response, _ = _post("/v1/messages", {**MESSAGES, "stream": stream}, replies, auth_header)
    assert "tool_use" not in response.text
    if stream:
        assert "event: error" in response.text
        assert "set_volume.level is required" in response.text
        return
    assert response.status_code == 502
    body = response.json()
    assert body["type"] == "error" and body["error"]["type"] == "api_error"
    assert "set_volume.level is required" in body["error"]["message"] and body["error"]["hint"]


@MODES
def test_chat_a_call_cut_off_by_the_token_limit_goes_out_as_it_came(
    stream, auth_header, openrouter_key
):
    """The model ran out of tokens mid-arguments. No re-ask, no 502: finish_reason stays length."""
    cut = call("set_volume", '{"level": 7, "room": "de', "c1")
    response, upstream = _post(
        "/v1/chat/completions",
        {**CHAT, "stream": stream},
        [Reply(calls=[cut], finish="length")],
        auth_header,
    )
    assert response.status_code == 200 and len(upstream.requests) == 1
    assert response.content == upstream.sent[0]
    assert b'"finish_reason": "length"' in response.content


@MODES
def test_messages_on_a_chat_backend_max_tokens_is_not_an_error(stream, auth_header, openrouter_key):
    cut = call("set_volume", '{"level": 7, "room": "de', "c1")
    response, upstream = _post(
        "/v1/messages",
        {**MESSAGES, "stream": stream},
        [Reply(calls=[cut], finish="length")],
        auth_header,
    )
    assert response.status_code == 200 and len(upstream.requests) == 1
    assert "event: error" not in response.text and '"type":"error"' not in response.text
    assert "max_tokens" in response.text


def test_the_messages_stream_error_event_carries_the_hint(auth_header, openrouter_key):
    replies = [Reply(calls=[call("set_volume", {"room": "den"}, "c1")])] * 2
    response, _ = _post("/v1/messages", {**MESSAGES, "stream": True}, replies, auth_header)
    [event] = [e for e in response.text.split("\n\n") if e.startswith("event: error")]
    error = json.loads(event.split("data: ", 1)[1])["error"]
    assert error["type"] == "api_error" and "NEXUS_TOOL_REPAIR" in error["hint"]


TWO_DEVICES = {"n1": [], "n2": []}
SHELL = {
    "properties": {"node_id": {"type": "string"}, "cmd": {"type": "string"}},
    "required": ["cmd"],
}


def _shell_ctx():
    return tr._Ctx(tools={"shell": SHELL}, nodes=TWO_DEVICES)


def _unlisted(id):
    return _raw("shell", {"cmd": "ls", "node_id": "elsewhere-" + id}, id)


def test_a_reask_with_fewer_advice_but_a_schema_problem_does_not_win():
    """Original: no schema problems, two unlisted device ids (advice only). It must go out."""
    original = {"role": "assistant", "tool_calls": [_unlisted("a"), _unlisted("b")]}
    worse = {"role": "assistant", "tool_calls": [_raw("shell", {"node_id": "n1"}, "c")]}

    async def reask(calls, problems):
        return worse

    repaired = asyncio.run(tr._repair_message(original, _shell_ctx(), reask))
    assert repaired is None, "the original answer goes out untouched"


def test_advice_breaks_a_tie_between_equally_valid_answers():
    original = {"role": "assistant", "tool_calls": [_unlisted("a")]}
    better = {
        "role": "assistant",
        "tool_calls": [_raw("shell", {"cmd": "ls", "node_id": "n1"}, "c")],
    }

    async def reask(calls, problems):
        return better

    repaired = asyncio.run(tr._repair_message(original, _shell_ctx(), reask))
    assert repaired is not None and repaired["tool_calls"][0]["id"] == "c"


@MODES
def test_chat_unlisted_device_ids_survive_a_worse_reask(stream, auth_header, openrouter_key):
    system = '<nodes><node id="n1" name="One"></node><node id="n2" name="Two"></node></nodes>'
    shell = {
        "type": "function",
        "function": {"name": "shell", "parameters": {**SHELL, "type": "object"}},
    }
    body = {
        **CHAT,
        "stream": stream,
        "tools": [shell],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": "go"}],
    }
    first = Reply(
        calls=[
            call("shell", {"cmd": "ls", "node_id": "x-unlisted-1"}, "c1"),
            call("shell", {"cmd": "ls", "node_id": "x-unlisted-2"}, "c2"),
        ]
    )
    second = Reply(calls=[call("shell", {"node_id": "n1"}, "c3")])
    response, upstream = _post("/v1/chat/completions", body, [first, second], auth_header)
    assert response.status_code == 200 and len(upstream.requests) == 2
    assert error_of(response.content) is None
    message = read_stream(response.content) if stream else response.json()["choices"][0]["message"]
    assert [c["id"] for c in message["tool_calls"]] == ["c1", "c2"]


@pytest.mark.parametrize(
    "value", ["\uff17", "\u0663", " 7", "7 ", "7\n", "9" * 400, "1." + "9" * 400, "1e99999"]
)
def test_only_plain_ascii_numbers_are_coerced(value):
    for kind in ("integer", "number"):
        fixes: set[str] = set()
        assert tr._normalize(value, {"type": kind}, fixes) == value
        assert fixes == set()


@pytest.mark.parametrize("keyword", ["allOf", "anyOf", "oneOf", "$ref"])
def test_unknown_keys_are_kept_when_the_schema_composes_other_schemas(keyword):
    schema = {"additionalProperties": False, "properties": {}, keyword: [{}]}
    value = {"from_a_branch": 1}
    tr._normalize(value, schema, set())
    assert value == {"from_a_branch": 1}


@pytest.mark.parametrize("arguments", ["{broken", "[1]"])
def test_a_hostile_tool_name_never_reaches_any_problem_text(arguments):
    raw = _raw("nope\x1b[31m\n\u202e" + "x" * 300, "")
    raw["function"]["arguments"] = arguments
    problems = tr._check([raw], _ctx()).problems
    for problem in problems:
        assert problem.isprintable() and "\x1b" not in problem and len(problem) < 400
    assert len(problems) == 2


def _tool_use_sse(name, partial):
    start = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}},
    }
    delta = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": partial},
    }
    return b"".join(
        [
            sse_event("content_block_start", start),
            sse_event("content_block_delta", delta),
            sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            sse_event("message_stop", {"type": "message_stop"}),
        ]
    )


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_native_anthropic_messages_are_forwarded_even_when_the_call_is_bad(
    stream, auth_header, monkeypatch
):
    monkeypatch.setenv("OS3_BRIDGE_BACKEND", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret-value")
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "nope", "input": {"x": 1}}],
        "stop_reason": "max_tokens",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 9},
    }
    answer = _tool_use_sse("nope", '{"level": "7",}') if stream else json.dumps(message).encode()
    media = "text/event-stream" if stream else "application/json"
    app = create_app(
        backend=AnthropicBackend(
            transport=HandlerTransport(
                lambda request: httpx.Response(200, content=answer, headers={"content-type": media})
            )
        )
    )
    body = {**MESSAGES, "model": "claude-sonnet-4-5", "stream": stream}
    with TestClient(app) as client:
        response = client.post("/v1/messages", json=body, headers=auth_header)
    assert response.status_code == 200
    assert response.content == answer
