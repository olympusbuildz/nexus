"""Check and repair the tool calls a model returns to OS3, before OS3 sees them.

OS3 sends strict tool schemas and runs every call it gets back; a weak model that
garbles one argument fails the whole step. This module sits on the response path of
POST /v1/chat/completions, only for requests that carry `tools`:

1. Deterministic repairs of failures OS3 would reject anyway: arguments that are not
   valid JSON (stray backslashes, fences, literal newlines, double encoding), a
   device name or garbled id where `node_id` wants an id, a `computer_use` script
   that is an action name, a capture with no feed_image after it, a missing call id.
2. Validation of what is left against the request's own tool schemas.
3. If calls are still invalid, ONE re-ask to the same model with the problems listed.
4. If they are still invalid after that, the client gets a clean `invalid_tool_call`
   error (HTTP 502, or an error event on a stream that already started) instead of a
   call it would fail on. Only schema problems reject; a device id that does not match
   the prompt's node list is advice for the re-ask and never rejects.

Rules this layer keeps:
- A response with nothing to fix goes out byte for byte.
- A bug in here returns the upstream response unchanged. Only a call that is still
  invalid after repair and one re-ask becomes an error.
- A re-ask is adopted only if it has strictly fewer problems and still calls tools.
- Missing required fields are never invented. The one fill is `node_id` when the
  prompt registers exactly one device.
- Message contents, arguments, and keys are never logged: only counts and fix kinds.

`open_chat_repaired` serves chat completions, and /v1/messages on backends that are
reached through chat. The native Anthropic backend's Messages are forwarded untouched.

The failure modes were chosen from what OS3 traffic is known to hit (see PR notes);
the implementation is this repo's own.
"""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, NoReturn

from nexus.backends.base import Backend, ChatUpstream
from nexus.config import tool_repair_enabled
from nexus.errors import GatewayError
from nexus.pulse import note_tool_repair

logger = logging.getLogger("nexus")

Reask = Callable[[list[dict[str, Any]], list[str]], Awaitable[dict[str, Any] | None]]

_SCRIPTS = ("probe.py", "capture.py", "act.py")
_ECHO_LIMIT = 1500
_NODE = re.compile(r"<node\b([^>]*)>(.*?)</node>", re.S)
_ATTR = re.compile(r'(?<![\w-])(id|name)="([^"]*)"')
_HOSTNAME = re.compile(r"<hostname>\s*([^<]*?)\s*</hostname>")
_EVENT_END = re.compile(rb"\r?\n\r?\n")
_FENCE = re.compile(r"^```[a-zA-Z]*\s*(.*?)\s*```$", re.S)
_INT = re.compile(r"-?[0-9]{1,30}")
_NUMBER = re.compile(r"-?[0-9]{1,30}(\.[0-9]{1,30})?([eE][+-]?[0-9]{1,4})?")
_OPEN_KEYS = frozenset({"patternProperties", "allOf", "anyOf", "oneOf", "$ref"})
_CLOSER = re.compile(r"\s*[}\]]")
_SHOWN_PROBLEMS = 5
_HINT = (
    "The model's tool call did not match your tools and could not be repaired. Retry the "
    "turn, or pick a stronger model. NEXUS_TOOL_REPAIR=off passes model output "
    "through unchecked."
)
_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


@dataclass(frozen=True)
class _Ctx:
    tools: dict[str, dict[str, Any]]
    nodes: dict[str, list[str]]


@dataclass
class _Checked:
    calls: list[dict[str, Any]]
    problems: list[str]
    fixes: list[str]
    advice: list[str]


async def open_chat_repaired(backend: Backend, payload: dict[str, Any]) -> ChatUpstream:
    """backend.open_chat, with tool calls in the answer checked and repaired."""
    upstream = await backend.open_chat(payload, stream=bool(payload.get("stream")))
    try:
        ctx = _context(payload)
    except Exception:
        ctx = None
    if ctx is None or upstream.status_code >= 400:
        return upstream
    reask = _reasker(backend, payload, ctx)
    if upstream.stream is not None:
        return ChatUpstream(
            status_code=upstream.status_code,
            media_type=upstream.media_type,
            stream=_guard_stream(upstream.stream, ctx, reask),
        )
    return await _repair_body(upstream, ctx, reask)


def _context(payload: dict[str, Any]) -> _Ctx | None:
    if not tool_repair_enabled() or payload.get("tool_choice") == "none":
        return None
    tools: dict[str, dict[str, Any]] = {}
    for item in payload.get("tools") or []:
        fn = item.get("function", item) if isinstance(item, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            params = fn.get("parameters")
            tools[fn["name"]] = params if isinstance(params, dict) else {}
    system = "\n".join(
        _text_of(m.get("content"))
        for m in payload.get("messages") or []
        if isinstance(m, dict) and m.get("role") in {"system", "developer"}
    )
    return _Ctx(tools=tools, nodes=_parse_nodes(system)) if tools else None


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


async def _repair_message(
    message: dict[str, Any], ctx: _Ctx, reask: Reask, finish: object = None
) -> dict[str, Any] | None:
    """The message to send instead, or None when the original should go out untouched.

    Raises GatewayError(invalid_tool_call) when a call is still invalid after the re-ask.
    An answer cut off by the token limit is left alone: its last call is incomplete, and
    the client already reads finish_reason `length` as such.
    """
    original = message.get("tool_calls")
    if not isinstance(original, list) or not original or finish == "length":
        return None
    checked = _check(original, ctx)
    source = message
    fixes = list(checked.fixes)
    if checked.problems or checked.advice:
        retry = await reask(checked.calls, [*checked.problems, *checked.advice])
        retry_calls = retry.get("tool_calls") if retry else None
        if retry is not None and isinstance(retry_calls, list) and retry_calls:
            second = _check(retry_calls, ctx)
            if _issues(second) < _issues(checked):
                checked, source, fixes = second, retry, [*second.fixes, "reask"]
            else:
                fixes.append("reask_rejected")
        else:
            fixes.append("reask_failed")
    if checked.problems:
        _reject(checked.problems, fixes)
    calls, fed = _add_feed(checked.calls, ctx)
    if fed:
        fixes.append("feed_image")
    if fixes:
        logger.info(
            "tool repair: fixes=%s problems_left=%d",
            ",".join(sorted(set(fixes))),
            len(checked.advice),
        )
    if calls == original:
        return None
    note_tool_repair("repaired")
    return {**source, "tool_calls": calls}


def _issues(checked: _Checked) -> tuple[int, int]:
    """Schema problems decide which answer is better; advice only breaks a tie."""
    return len(checked.problems), len(checked.advice)


def _reject(problems: list[str], fixes: list[str]) -> NoReturn:
    logger.warning(
        "tool repair: rejected code=invalid_tool_call problems=%d fixes=%s",
        len(problems),
        ",".join(sorted(set(fixes))) or "-",
    )
    note_tool_repair("rejected")
    raise _invalid_tool_call(problems)


def _invalid_tool_call(problems: list[str]) -> GatewayError:
    shown = "; ".join(problems[:_SHOWN_PROBLEMS])
    more = len(problems) - _SHOWN_PROBLEMS
    if more > 0:
        shown += f"; and {more} more"
    return GatewayError(
        502,
        f"The model returned a tool call that does not match the request's tools: {shown}",
        "upstream_error",
        "invalid_tool_call",
        extra={"hint": _HINT},
    )


def _safe(name: str) -> str:
    """A model-written tool name, fit to appear in an error message."""
    return "".join(c if c.isascii() and (c.isalnum() or c in "_.:-") else "?" for c in name[:64])


def _check(raw_calls: list[Any], ctx: _Ctx) -> _Checked:
    fixes: list[str] = []
    problems: list[str] = []
    advice: list[str] = []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_calls:
        fn = raw.get("function") if isinstance(raw, dict) else None
        if not isinstance(fn, dict):
            problems.append("a tool call has no function object")
            if isinstance(raw, dict):
                out.append(raw)
            continue
        call = dict(raw)
        fn = dict(fn)
        call["function"] = fn
        if call.get("type") != "function":
            call["type"] = "function"
            fixes.append("type")
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in seen:
            call["id"] = "call_" + secrets.token_hex(12)
            fixes.append("id")
        seen.add(call["id"])
        name = fn.get("name") if isinstance(fn.get("name"), str) else ""
        if name not in ctx.tools:
            resolved = _resolve_name(name, ctx.tools)
            if resolved is not None:
                name = fn["name"] = resolved
                fixes.append("tool_name")
        schema = ctx.tools.get(name)
        if schema is None:
            known = ", ".join(sorted(ctx.tools))
            problems.append(f"there is no tool named {_safe(name)!r}; the tools are: {known}")
        args, repaired = _parse_args(fn.get("arguments"))
        if args is _BAD:
            problems.append(f"{_safe(name)}: arguments are not valid JSON")
        elif not isinstance(args, dict):
            problems.append(f"{_safe(name)}: arguments must be a JSON object")
        else:
            changed = repaired
            if schema is not None:
                normalized: set[str] = set()
                _normalize(args, schema, normalized)
                if normalized:
                    changed = True
                    fixes.extend(normalized)
                if _fix_node_id(args, schema, ctx.nodes):
                    changed = True
                    fixes.append("node_id")
                if name == "computer_use" and _fix_computer_use(args):
                    changed = True
                    fixes.append("computer_use")
                problems.extend(_schema_problems(args, schema, name))
                advice.extend(_node_advice(name, args, schema, ctx.nodes))
            if repaired:
                fixes.append("args")
            if changed:
                fn["arguments"] = json.dumps(args, ensure_ascii=False)
        out.append(call)
    return _Checked(out, problems, fixes, advice)


def _resolve_name(name: str, tools: dict[str, Any]) -> str | None:
    """The one tool `name` names in another case or with stray spaces. Never a prefix."""
    key = name.strip().casefold()
    same = [t for t in tools if t.casefold() == key]
    return same[0] if key and len(same) == 1 else None


_BAD: Any = object()


def _parse_args(raw: object) -> tuple[Any, bool]:
    """(value, repaired). repaired is True when `raw` was not already a JSON object string."""
    if raw is None:
        return {}, True
    if isinstance(raw, dict):
        return raw, True
    if not isinstance(raw, str):
        return _BAD, False
    text = raw.strip()
    if not text:
        return {}, True
    try:
        value = json.loads(text)
    except ValueError:
        pass
    else:
        if isinstance(value, str):
            inner, _ = _parse_args(value)
            return inner, True
        return value, False
    fence = _FENCE.match(text)
    if fence:
        text = fence.group(1)
    for candidate in (text, _escape_stray_backslashes(text)):
        for attempt in (candidate, _drop_trailing_commas(candidate)):
            try:
                return json.loads(attempt, strict=False), True
            except ValueError:
                pass
            value = _leading_object(attempt)
            if value is not _BAD:
                return value, True
    return _BAD, False


def _leading_object(text: str) -> Any:
    """The JSON object `text` starts with, when only junk follows it (`}`, a stray word).

    A second object or array after it is not junk: dropping that would lose arguments.
    """
    if not text.startswith("{"):
        return _BAD
    try:
        value, end = json.JSONDecoder(strict=False).raw_decode(text)
    except ValueError:
        return _BAD
    return value if isinstance(value, dict) and not re.search(r"[{\[]", text[end:]) else _BAD


def _drop_trailing_commas(text: str) -> str:
    """Remove a comma that sits right before a closing bracket, outside any string."""
    out: list[str] = []
    in_string = escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "," and _CLOSER.match(text, i + 1):
            continue
        out.append(ch)
    return "".join(out)


def _escape_stray_backslashes(text: str) -> str:
    """Models write shell escapes like `\\(` raw. Double any backslash JSON cannot read."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1 : i + 2]
        hex4 = re.fullmatch(r"[0-9a-fA-F]{4}", text[i + 2 : i + 6]) if nxt == "u" else None
        if (nxt and nxt in '"\\/bfnrt') or hex4:
            out.append(text[i : i + 2])
            i += 2
        else:
            out.append("\\\\")
            i += 1
    return "".join(out)


def _parse_nodes(text: str) -> dict[str, list[str]]:
    """Device id -> names it can be called, from the <node id=".." name=".."> list in the prompt."""
    nodes: dict[str, list[str]] = {}
    for attrs, body in _NODE.findall(text):
        found = dict(_ATTR.findall(attrs))
        if not found.get("id"):
            continue
        labels = [found.get("name", "")]
        host = _HOSTNAME.search(body)
        if host:
            labels += [host.group(1), host.group(1).split(".")[0]]
        nodes[found["id"]] = [label for label in labels if label]
    return nodes


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _fix_node_id(args: dict[str, Any], schema: dict[str, Any], nodes: dict[str, list[str]]) -> bool:
    """Replace a device name or a garbled id with the id it clearly means. Never guesses."""
    props = schema.get("properties")
    if not nodes or not isinstance(props, dict) or "node_id" not in props:
        return False
    value = args.get("node_id")
    if isinstance(value, str) and value in nodes:
        return False
    if value in (None, ""):
        if len(nodes) != 1:
            return False
        args["node_id"] = next(iter(nodes))
        return True
    if not isinstance(value, str):
        return False
    key = _norm(value)
    named = [nid for nid, labels in nodes.items() if key in {_norm(x) for x in [nid, *labels]}]
    if not named:
        named = [
            nid
            for nid in nodes
            if len(nid) == len(value) and sum(a != b for a, b in zip(nid, value, strict=True)) <= 3
        ]
    if len(named) != 1:
        return False
    args["node_id"] = named[0]
    return True


def _fix_computer_use(args: dict[str, Any]) -> bool:
    """computer_use runs probe.py, capture.py or act.py. Models name an act.py action instead."""
    script = args.get("script")
    rest = args.get("args", [])
    if not isinstance(script, str) or not script.strip() or script in _SCRIPTS:
        return False
    if not isinstance(rest, list):
        return False
    base = script.strip().rsplit("/", 1)[-1]
    if base in _SCRIPTS:
        args["script"] = base
    elif base + ".py" in _SCRIPTS:
        args["script"] = base + ".py"
    else:
        args["script"] = "act.py"
        args["args"] = [base, *rest]
    return True


def _add_feed(calls: list[dict[str, Any]], ctx: _Ctx) -> tuple[list[dict[str, Any]], bool]:
    """A screenshot nobody looks at is wasted: follow the last capture with feed_image."""
    schema = ctx.tools.get("feed_image")
    if schema is None:
        return calls, False
    last: tuple[int, str, Any] | None = None
    for i, call in enumerate(calls):
        args = _args_of(call, "computer_use")
        rest = args.get("args") if args else None
        if not args or args.get("script") != "capture.py" or not isinstance(rest, list):
            continue
        if "--out" in rest and rest.index("--out") + 1 < len(rest):
            path = rest[rest.index("--out") + 1]
            if isinstance(path, str) and path:
                last = (i, path, args.get("node_id"))
    if last is None:
        return calls, False
    index, path, node = last
    if any(_name_of(call) == "feed_image" for call in calls[index + 1 :]):
        return calls, False
    feed: dict[str, Any] = {"reference": path}
    if isinstance(node, str) and node:
        feed["node_id"] = node
    if _schema_problems(feed, schema, "feed_image"):
        return calls, False
    added = {
        "id": "call_" + secrets.token_hex(12),
        "type": "function",
        "function": {"name": "feed_image", "arguments": json.dumps(feed)},
    }
    return [*calls, added], True


def _name_of(call: dict[str, Any]) -> object:
    fn = call.get("function")
    return fn.get("name") if isinstance(fn, dict) else None


def _args_of(call: dict[str, Any], name: str) -> dict[str, Any] | None:
    if _name_of(call) != name:
        return None
    try:
        args = json.loads(call["function"].get("arguments") or "{}")
    except (ValueError, TypeError):
        return None
    return args if isinstance(args, dict) else None


def _node_advice(
    name: str, args: dict[str, Any], schema: dict[str, Any], nodes: dict[str, list[str]]
) -> list[str]:
    """A device id the prompt does not list. Worth a re-ask, but the prompt parse is a
    heuristic, so it never rejects a call."""
    props = schema.get("properties")
    node = args.get("node_id")
    if nodes and isinstance(props, dict) and "node_id" in props and isinstance(node, str):
        if node not in nodes:
            return [f"{name}.node_id must be the id of a registered device"]
    return []


def _normalize(value: Any, schema: object, fixes: set[str]) -> Any:
    """Coerce numeric strings where the schema wants a number; drop unknown keys where it
    sets additionalProperties:false. Mutates containers, returns the (coerced) value."""
    if not isinstance(schema, dict):
        return value
    if isinstance(value, str):
        number = _as_number(value, schema)
        if number is not None:
            fixes.add("coerce")
            return number
    elif isinstance(value, dict):
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        if schema.get("additionalProperties") is False and not _OPEN_KEYS.intersection(schema):
            for key in [k for k in value if k not in props]:
                del value[key]
                fixes.add("extra_args")
        for key in [k for k in value if k in props]:
            value[key] = _normalize(value[key], props[key], fixes)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        value[:] = [_normalize(item, schema["items"], fixes) for item in value]
    return value


def _as_number(text: str, schema: dict[str, Any]) -> int | float | None:
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else kinds if isinstance(kinds, list) else []
    enum = schema.get("enum")
    if "string" in kinds or (isinstance(enum, list) and text in enum):
        return None
    if "integer" in kinds and _INT.fullmatch(text):
        return int(text)
    if "number" in kinds and _NUMBER.fullmatch(text):
        number = int(text) if _INT.fullmatch(text) else float(text)
        return number if math.isfinite(number) else None
    return None


def _type_ok(value: object, kind: str) -> bool:
    expected = _TYPES.get(kind)
    if expected is None:
        return True
    if kind in {"integer", "number"} and isinstance(value, bool):
        return False
    if kind == "integer" and isinstance(value, float):
        return value.is_integer()
    return isinstance(value, expected)


def _schema_problems(value: object, schema: object, path: str) -> list[str]:
    """The subset of JSON Schema OS3 tools use: type, enum, required, properties, items."""
    if not isinstance(schema, dict):
        return []
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else kinds if isinstance(kinds, list) else []
    if kinds and not any(_type_ok(value, k) for k in kinds if isinstance(k, str)):
        return [f"{path} must be {' or '.join(str(k) for k in kinds)}"]
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return [f"{path} must be one of {enum}"]
    out: list[str] = []
    if isinstance(value, dict):
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        out += [f"{path}.{key} is required" for key in required if key not in value]
        for key, item in value.items():
            if key in props:
                out += _schema_problems(item, props[key], f"{path}.{key}")
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            out += _schema_problems(item, schema["items"], f"{path}[{i}]")
    return out


def _reasker(backend: Backend, payload: dict[str, Any], ctx: _Ctx) -> Reask:
    async def reask(calls: list[dict[str, Any]], problems: list[str]) -> dict[str, Any] | None:
        try:
            retry = _reask_payload(payload, ctx, calls, problems)
            upstream = await backend.open_chat(retry, stream=False)
            if upstream.status_code >= 400 or upstream.body is None:
                return None
            message = json.loads(upstream.body)["choices"][0]["message"]
            return message if isinstance(message, dict) else None
        except Exception:
            return None

    return reask


def _reask_payload(
    payload: dict[str, Any], ctx: _Ctx, calls: list[dict[str, Any]], problems: list[str]
) -> dict[str, Any]:
    """The same request plus what the model said and why OS3 would reject it, as plain text.

    Plain text, not a tool message: an invalid-JSON `arguments` string in history is
    refused outright by some providers.
    """
    shown = []
    for call in calls:
        fn = call.get("function") or {}
        args = fn.get("arguments")
        shown.append(f"{fn.get('name')}({str(args)[:_ECHO_LIMIT]})")
    note = ["These tool calls were rejected before they ran:", *[f"- {p}" for p in problems[:8]]]
    if ctx.nodes:
        note.append("Registered device ids: " + ", ".join(ctx.nodes))
    note.append("Reply again with corrected tool calls only.")
    retry = {k: v for k, v in payload.items() if k != "stream_options"}
    retry["stream"] = False
    retry["messages"] = [
        *payload["messages"],
        {"role": "assistant", "content": "Tool calls I attempted:\n" + "\n".join(shown)},
        {"role": "user", "content": "\n".join(note)},
    ]
    return retry


async def _repair_body(upstream: ChatUpstream, ctx: _Ctx, reask: Reask) -> ChatUpstream:
    try:
        completion = json.loads(upstream.body or b"")
        choice = completion["choices"][0]
        repaired = await _repair_message(choice["message"], ctx, reask, choice.get("finish_reason"))
        if repaired is None:
            return upstream
        choice["message"] = repaired
        if choice.get("finish_reason") in {None, "stop"}:
            choice["finish_reason"] = "tool_calls"
        body = json.dumps(completion, ensure_ascii=False).encode("utf-8")
    except GatewayError:
        raise
    except Exception:
        return upstream
    return ChatUpstream(status_code=upstream.status_code, media_type=upstream.media_type, body=body)


async def _sse_events(stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    pending = b""
    try:
        async for chunk in stream:
            pending += chunk
            while True:
                end = _EVENT_END.search(pending)
                if end is None:
                    break
                yield pending[: end.end()]
                pending = pending[end.end() :]
        if pending.strip():
            yield pending
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()


def _event_json(event: bytes) -> dict[str, Any] | None:
    for line in event.splitlines():
        if line.startswith(b"data:"):
            try:
                value = json.loads(line[5:].strip())
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def _delta_of(obj: dict[str, Any]) -> dict[str, Any]:
    choices = obj.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    delta = choice.get("delta") if isinstance(choice, dict) else None
    return delta if isinstance(delta, dict) else {}


async def _guard_stream(
    stream: AsyncIterator[bytes], ctx: _Ctx, reask: Reask
) -> AsyncIterator[bytes]:
    held: list[bytes] = []
    async for event in _sse_events(stream):
        if held:
            held.append(event)
            continue
        obj = _event_json(event)
        if obj is not None and _delta_of(obj).get("tool_calls"):
            held.append(event)
        else:
            yield event
    if held:
        for event in await _release(held, ctx, reask):
            yield event


async def _release(held: list[bytes], ctx: _Ctx, reask: Reask) -> list[bytes]:
    try:
        parsed = [(event, _event_json(event)) for event in held]
        message, head = _assemble([obj for _, obj in parsed if obj is not None])
        repaired = await _repair_message(message, ctx, reask, head.get("finish"))
        if repaired is None:
            return held
        return _synthesize(repaired, head)
    except GatewayError as exc:
        return [_sse({"error": exc.as_dict()["error"]}), b"data: [DONE]\n\n"]
    except Exception:
        return held


def _assemble(objs: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fold chunk deltas into one assistant message, plus id/model/created/finish for new chunks."""
    head: dict[str, Any] = {}
    text: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    last: int | None = None
    for obj in objs:
        for key in ("id", "model", "created"):
            if key in obj and key not in head:
                head[key] = obj[key]
        if isinstance(obj.get("usage"), dict):
            head["usage"] = obj["usage"]
        choices = obj.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            if choices[0].get("finish_reason"):
                head["finish"] = choices[0]["finish_reason"]
        delta = _delta_of(obj)
        if isinstance(delta.get("content"), str):
            text.append(delta["content"])
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            index = tc.get("index")
            if not isinstance(index, int):
                fresh = last is None or bool(tc.get("id"))
                index = (max(calls) + 1 if calls else 0) if fresh else last
            last = index
            entry = calls.setdefault(
                index,
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if isinstance(tc.get("id"), str) and not entry["id"]:
                entry["id"] = tc["id"]
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            if isinstance(fn.get("name"), str) and not entry["function"]["name"]:
                entry["function"]["name"] = fn["name"]
            if isinstance(fn.get("arguments"), str):
                entry["function"]["arguments"] += fn["arguments"]
            elif isinstance(fn.get("arguments"), dict):
                entry["function"]["arguments"] = json.dumps(fn["arguments"])
    message = {
        "role": "assistant",
        "content": "".join(text) or None,
        "tool_calls": [calls[i] for i in sorted(calls)],
    }
    return message, head


def _sse(chunk: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n"


def _synthesize(message: dict[str, Any], head: dict[str, Any]) -> list[bytes]:
    base = {
        "id": head.get("id", "chatcmpl-nexus"),
        "object": "chat.completion.chunk",
        "created": head.get("created", int(time.time())),
        "model": head.get("model", ""),
    }
    delta: dict[str, Any] = {"role": "assistant"}
    if message.get("content"):
        delta["content"] = message["content"]
    delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
    chunks = [
        {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    if isinstance(head.get("usage"), dict):
        chunks.append({**base, "choices": [], "usage": head["usage"]})
    events = [
        b"data: " + json.dumps(c, ensure_ascii=False).encode("utf-8") + b"\n\n" for c in chunks
    ]
    return [*events, b"data: [DONE]\n\n"]
