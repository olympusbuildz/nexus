"""A CLI failure reads exactly as it did before the fallback chain existed.

The goldens were produced by `subscription._failure` on main. The wider quota patterns
live in the failover classifier only, so a CLI error that merely mentions "quota" is
still a 502 `<id>_failed`, never a 429 `<id>_usage_limit`, whether or not a chain is set.
"""

from __future__ import annotations

import json

import pytest
from nexus.backends import subscription as sub

CLASSES = {
    "codex": sub.CodexCliBackend,
    "claude-code": sub.ClaudeCodeBackend,
    "copilot": sub.CopilotCliBackend,
    "grok-build": sub.GrokBuildBackend,
    "antigravity": sub.AntigravityBackend,
}
HINT_5XX = "The upstream or bridge failed. GET /doctor or ./doctor.sh shows the cause."
HINT_429 = "The upstream is rate limiting or out of quota. Wait and retry, or GET /doctor."

F, U = "api_error", "rate_limit_error"
GOLDEN = [
    ("grok-build", "quota exceeded for this model", "", 502, F, "grok-build_failed"),
    ("grok-build", "Rate limit reached for grok-build", "", 502, F, "grok-build_failed"),
    ("copilot", "You have exceeded your premium request allowance", "", 502, F, "copilot_failed"),
    ("antigravity", "RESOURCE_EXHAUSTED: Quota exceeded", "", 502, F, "antigravity_failed"),
    ("codex", "rate limit", "", 502, F, "codex_failed"),
    ("grok-build", "", '{"result":"see the quota docs"}', 502, F, "grok-build_failed"),
    ("codex", "ERROR: You've hit your usage limit.", "", 429, U, "codex_usage_limit"),
    ("claude-code", "Claude AI usage limit reached|17", "", 429, U, "claude-code_usage_limit"),
    ("codex", "subscription rate limit hit", "", 429, U, "codex_usage_limit"),
    ("codex", "codex exploded sk-proj-secret", "", 502, F, "codex_failed"),
    ("codex", "", "", 502, F, "codex_empty"),
    ("codex", "timed out", "", 504, F, "codex_timeout"),
    ("codex", "Not logged in", "", 503, "codex_unavailable", "codex_login_required"),
]

MESSAGES = {
    "codex exploded sk-proj-secret": "codex exploded [REDACTED]",
    "": "Codex CLI returned no assistant text.",
    "timed out": "Codex CLI timed out",
    "Not logged in": (
        "Codex CLI is not logged in. Run `codex login` in a terminal. "
        "This bridge does not store that login."
    ),
}


@pytest.mark.parametrize(("backend", "stderr", "stdout", "status", "kind", "code"), GOLDEN)
def test_cli_failure_classification_is_unchanged(
    backend: str, stderr: str, stdout: str, status: int, kind: str, code: str
) -> None:
    exc = CLASSES[backend]()._failure(1 if stderr != "timed out" else 124, stderr, stdout, None)
    body = exc.as_dict()["error"]
    assert (exc.status_code, body["type"], body["code"]) == (status, kind, code)
    assert body["hint"] == (HINT_429 if status == 429 else HINT_5XX)
    assert set(body) == {"message", "type", "code", "hint"}
    expected = MESSAGES.get(stderr, stderr) if (stderr or not stdout) else stdout
    assert body["message"] == expected
    json.dumps(body)
