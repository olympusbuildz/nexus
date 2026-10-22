"""Scripted upstream answers for the OS3 replay: clean controls and the malformed calls
weak models produce. Each case says what the model sends, and what OS3 should end up with.

kind:
  control  valid answer. Must pass through byte for byte, repair on or off.
  fixed    invalid as sent. With repair on, OS3 gets exactly `expect`.
  rejected invalid and not repairable, even after the one re-ask. OS3 gets a clean
           `invalid_tool_call` error (HTTP 502, or an error event on a stream) and no call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tests.os3_replay import Call, Reply, call

STUDIO = "7c1d2e4a-5b6f-4a8c-9d0e-1f2a3b4c5d6e"
LAPTOP = "b3a9c8d7-e6f5-4b4a-8c3d-2e1f0a9b8c7d"
GARBLED_STUDIO = "7c1d2e4b-5b6f-4a8c-9d0e-1f2a3b4c5d9e"

_FIND = "find ~/Downloads -name '*.dmg' \\( -size +1M \\) -print"
_SHOT = "/tmp/os3-shot-2.png"


@dataclass(frozen=True)
class Case:
    id: str
    fixture: str
    summary: str
    kind: str
    replies: list[Reply]
    expect: list[tuple[str, Any]]
    upstream_calls: int = 1


def _one(*calls: Call, content: str | None = None) -> list[Reply]:
    return [Reply(calls=list(calls), content=content)]


def _shell(command: str = "uptime", node_id: str = STUDIO) -> dict[str, str]:
    return {"command": command, "node_id": node_id}


CASES: list[Case] = [
    Case(
        "chat-clean",
        "chat",
        "valid create_task",
        "control",
        _one(
            call(
                "create_task",
                {"title": "Tidy", "instructions": "Trash old .dmg files.", "node_id": STUDIO},
            )
        ),
        [
            (
                "create_task",
                {"title": "Tidy", "instructions": "Trash old .dmg files.", "node_id": STUDIO},
            )
        ],
    ),
    Case(
        "chat-text-answer", "chat", "plain text answer", "control", _one(content="Sure, on it."), []
    ),
    Case(
        "chat-device-name",
        "chat",
        "node_id is the device name, `Studio Mini`",
        "fixed",
        _one(
            call(
                "create_task",
                {
                    "title": "Tidy",
                    "instructions": "Trash old .dmg files.",
                    "node_id": "Studio Mini",
                },
            )
        ),
        [
            (
                "create_task",
                {"title": "Tidy", "instructions": "Trash old .dmg files.", "node_id": STUDIO},
            )
        ],
    ),
    Case(
        "chat-hostname",
        "chat",
        "node_id is the hostname, `studio-mini.local`",
        "fixed",
        _one(
            call("notify_before_act", {"message": "Starting."}, "call_n1"),
            call(
                "create_task",
                {"title": "Tidy", "instructions": "Go.", "node_id": "studio-mini.local"},
                "call_n2",
            ),
        ),
        [
            ("notify_before_act", {"message": "Starting."}),
            ("create_task", {"title": "Tidy", "instructions": "Go.", "node_id": STUDIO}),
        ],
    ),
    Case(
        "chat-no-id-empty-args",
        "chat",
        "no tool call id, empty arguments",
        "fixed",
        _one(call("get_setup_status", "", None)),
        [("get_setup_status", {})],
    ),
    Case(
        "worker-clean",
        "worker",
        "valid shell call",
        "control",
        _one(call("shell", _shell("du -ch ~/Downloads/*.dmg"))),
        [("shell", _shell("du -ch ~/Downloads/*.dmg"))],
    ),
    Case(
        "worker-bad-escape",
        "worker",
        r"`\(` unescaped in the JSON string",
        "fixed",
        _one(call("shell", '{"command": "' + _FIND + '", "node_id": "' + STUDIO + '"}')),
        [("shell", _shell(_FIND))],
    ),
    Case(
        "worker-garbled-id",
        "worker",
        "node_id with two characters wrong",
        "fixed",
        _one(call("shell", _shell(node_id=GARBLED_STUDIO))),
        [("shell", _shell())],
    ),
    Case(
        "worker-parallel-mixed",
        "worker",
        "three calls in one turn: one fine, one bad escape, one device name",
        "fixed",
        _one(
            call("notify_before_act", {"message": "Looking."}, "call_p1"),
            call("shell", '{"command": "' + _FIND + '", "node_id": "' + STUDIO + '"}', "call_p2"),
            call("file_read", {"path": "/tmp/a.txt", "node_id": "laptop"}, "call_p3"),
        ),
        [
            ("notify_before_act", {"message": "Looking."}),
            ("shell", _shell(_FIND)),
            ("file_read", {"path": "/tmp/a.txt", "node_id": LAPTOP}),
        ],
    ),
    Case(
        "worker-literal-newline",
        "worker",
        "raw newline inside a JSON string",
        "fixed",
        _one(
            call(
                "file_write",
                '{"path": "/tmp/n.txt", "content": "one\ntwo", "node_id": "' + STUDIO + '"}',
            )
        ),
        [("file_write", {"path": "/tmp/n.txt", "content": "one\ntwo", "node_id": STUDIO})],
    ),
    Case(
        "worker-fenced-args",
        "worker",
        "arguments wrapped in a ```json fence",
        "fixed",
        _one(
            call(
                "shell",
                "```json\n" + '{"command": "uptime", "node_id": "' + STUDIO + '"}' + "\n```",
            )
        ),
        [("shell", _shell())],
    ),
    Case(
        "worker-double-encoded",
        "worker",
        "arguments JSON-encoded twice",
        "fixed",
        _one(call("shell", '"{\\"command\\": \\"uptime\\", \\"node_id\\": \\"' + STUDIO + '\\"}"')),
        [("shell", _shell())],
    ),
    Case(
        "worker-trailing-comma",
        "worker",
        "trailing comma before the closing brace",
        "fixed",
        _one(call("shell", '{"command": "uptime", "node_id": "' + STUDIO + '",}')),
        [("shell", _shell())],
    ),
    Case(
        "worker-trailing-junk",
        "worker",
        "arguments followed by a stray closing tag",
        "fixed",
        _one(call("shell", '{"command": "uptime", "node_id": "' + STUDIO + '"}\n</tool_call>')),
        [("shell", _shell())],
    ),
    Case(
        "worker-tool-name-case",
        "worker",
        "tool name `Shell` for `shell`",
        "fixed",
        _one(call("Shell", _shell())),
        [("shell", _shell())],
    ),
    Case(
        "worker-numeric-string",
        "worker",
        'integer `timeout_ms` sent as the string `"30000"`',
        "fixed",
        _one(call("shell", {**_shell(), "timeout_ms": "30000"})),
        [("shell", {**_shell(), "timeout_ms": 30000})],
    ),
    Case(
        "worker-missing-command",
        "worker",
        "required `command` missing (re-ask fixes it)",
        "fixed",
        [Reply(calls=[call("shell", {"node_id": STUDIO})]), Reply(calls=[call("shell", _shell())])],
        [("shell", _shell())],
        upstream_calls=2,
    ),
    Case(
        "worker-unknown-tool",
        "worker",
        "calls a tool that is not offered, `bash` (re-ask fixes it)",
        "fixed",
        [Reply(calls=[call("bash", _shell())]), Reply(calls=[call("shell", _shell())])],
        [("shell", _shell())],
        upstream_calls=2,
    ),
    Case(
        "worker-missing-node-two-devices",
        "worker",
        "no node_id with two devices registered; cannot guess (re-ask fixes it)",
        "fixed",
        [
            Reply(calls=[call("shell", {"command": "uptime"})]),
            Reply(calls=[call("shell", _shell())]),
        ],
        [("shell", _shell())],
        upstream_calls=2,
    ),
    Case(
        "worker-bad-enum",
        "worker",
        "todo status `doing` is not in the enum (re-ask fixes it)",
        "fixed",
        [
            Reply(calls=[call("todo", {"items": [{"text": "list", "status": "doing"}]})]),
            Reply(calls=[call("todo", {"items": [{"text": "list", "status": "in_progress"}]})]),
        ],
        [("todo", {"items": [{"text": "list", "status": "in_progress"}]})],
        upstream_calls=2,
    ),
    Case(
        "worker-reask-http-500",
        "worker",
        "bad tool name, and the re-ask hits an upstream 500",
        "rejected",
        [Reply(calls=[call("bash", _shell())]), Reply(status=500)],
        [],
        upstream_calls=2,
    ),
    Case(
        "worker-reask-no-better",
        "worker",
        "empty shell call, and the re-ask is just as empty",
        "rejected",
        [Reply(calls=[call("shell", {})]), Reply(calls=[call("shell", {})])],
        [],
        upstream_calls=2,
    ),
    Case(
        "worker-reask-worse",
        "worker",
        "missing node_id, and the re-ask drops the command too",
        "rejected",
        [Reply(calls=[call("shell", {"command": "ls"})]), Reply(calls=[call("shell", {})])],
        [],
        upstream_calls=2,
    ),
    Case(
        "worker-reask-gives-up",
        "worker",
        "bad tool name, and the re-ask answers in text",
        "rejected",
        [Reply(calls=[call("bash", _shell())]), Reply(content="Sorry, I cannot do that.")],
        [],
        upstream_calls=2,
    ),
    Case(
        "worker-unparseable-args",
        "worker",
        "arguments that are not JSON, and the re-ask is no better",
        "rejected",
        [Reply(calls=[call("shell", "{broken")]), Reply(calls=[call("shell", "{broken")])],
        [],
        upstream_calls=2,
    ),
    Case(
        "worker-wrong-type",
        "worker",
        "`timeout_ms` is the word `soon`, and the re-ask repeats it",
        "rejected",
        [
            Reply(calls=[call("shell", {**_shell(), "timeout_ms": "soon"})]),
            Reply(calls=[call("shell", {**_shell(), "timeout_ms": "soon"})]),
        ],
        [],
        upstream_calls=2,
    ),
    Case(
        "worker-tool-name-prefix",
        "worker",
        "tool name `file_re`, only a prefix of `file_read`: never guessed (re-ask repeats it)",
        "rejected",
        [
            Reply(calls=[call("file_re", {"path": "/tmp/a.txt", "node_id": STUDIO})]),
            Reply(calls=[call("file_re", {"path": "/tmp/a.txt", "node_id": STUDIO})]),
        ],
        [],
        upstream_calls=2,
    ),
    Case(
        "cu-capture-and-feed",
        "computer_use",
        "capture followed by feed_image",
        "control",
        _one(
            call(
                "computer_use",
                {"node_id": STUDIO, "script": "capture.py", "args": ["--out", _SHOT]},
                "call_c1",
            ),
            call("feed_image", {"reference": _SHOT, "node_id": STUDIO}, "call_c2"),
        ),
        [
            ("computer_use", {"node_id": STUDIO, "script": "capture.py", "args": ["--out", _SHOT]}),
            ("feed_image", {"reference": _SHOT, "node_id": STUDIO}),
        ],
    ),
    Case(
        "cu-capture-no-feed",
        "computer_use",
        "screenshot taken, never looked at",
        "fixed",
        _one(
            call(
                "computer_use",
                {"node_id": STUDIO, "script": "capture.py", "args": ["--out", _SHOT]},
            )
        ),
        [
            ("computer_use", {"node_id": STUDIO, "script": "capture.py", "args": ["--out", _SHOT]}),
            ("feed_image", {"reference": _SHOT, "node_id": STUDIO}),
        ],
    ),
    Case(
        "cu-action-as-script",
        "computer_use",
        "`left_click` given as the script",
        "fixed",
        _one(
            call(
                "computer_use", {"node_id": STUDIO, "script": "left_click", "args": ["640", "360"]}
            )
        ),
        [
            (
                "computer_use",
                {"node_id": STUDIO, "script": "act.py", "args": ["left_click", "640", "360"]},
            )
        ],
    ),
    Case(
        "cu-capture-sloppy",
        "computer_use",
        "script `capture`, hostname as node_id, no feed_image",
        "fixed",
        _one(
            call(
                "computer_use",
                {"node_id": "Studio-Mini.local", "script": "capture", "args": ["--out", _SHOT]},
            )
        ),
        [
            ("computer_use", {"node_id": STUDIO, "script": "capture.py", "args": ["--out", _SHOT]}),
            ("feed_image", {"reference": _SHOT, "node_id": STUDIO}),
        ],
    ),
    Case(
        "cu-feed-device-name",
        "computer_use",
        "feed_image with the device name as node_id",
        "fixed",
        _one(call("feed_image", {"reference": "/tmp/os3-shot-1.png", "node_id": "Studio Mini"})),
        [("feed_image", {"reference": "/tmp/os3-shot-1.png", "node_id": STUDIO})],
    ),
    Case(
        "cu-feed-missing-reference",
        "computer_use",
        "feed_image without `reference` (re-ask fixes it)",
        "fixed",
        [
            Reply(calls=[call("feed_image", {"node_id": STUDIO})]),
            Reply(
                calls=[call("feed_image", {"reference": "/tmp/os3-shot-1.png", "node_id": STUDIO})]
            ),
        ],
        [("feed_image", {"reference": "/tmp/os3-shot-1.png", "node_id": STUDIO})],
        upstream_calls=2,
    ),
]
