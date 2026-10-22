"""Read the provider's own token counts out of a streamed response as it passes.

A streamed chat or Messages response carries its usage in the stream itself: an
OpenAI ``include_usage`` chunk (``"choices": []`` plus ``usage``), a final chunk with
``usage`` (the ChatGPT plan translation), or Anthropic ``message_start`` /
``message_delta`` events. ``SseUsage`` passes every event through unchanged, keeps the
numbers, and reports them with ``pulse.note_tokens`` when the stream ends.

When Nexus asked the upstream for ``include_usage`` on the client's behalf, the extra
usage-only chunk is dropped so the client sees the stream it asked for.
"""

from __future__ import annotations

import json
from typing import Any

from nexus.pulse import note_tokens

_MAX_BUFFER = 1 << 20


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0, int(value))


class SseUsage:
    def __init__(self, *, drop_usage_only: bool = False) -> None:
        self.drop_usage_only = drop_usage_only
        self.prompt: int | None = None
        self.completion: int | None = None
        self._buffer = b""

    @property
    def seen(self) -> bool:
        return self.prompt is not None or self.completion is not None

    def _take(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return
        prompt = _count(usage.get("prompt_tokens", usage.get("input_tokens")))
        completion = _count(usage.get("completion_tokens", usage.get("output_tokens")))
        if prompt is not None:
            self.prompt = max(self.prompt or 0, prompt)
        if completion is not None:
            self.completion = max(self.completion or 0, completion)

    def _event(self, raw: bytes) -> bool:
        """Read one event. True when it should be dropped."""
        data = [line[5:].strip() for line in raw.split(b"\n") if line.startswith(b"data:")]
        if len(data) != 1 or not data[0].startswith(b"{"):
            return False
        try:
            event: Any = json.loads(data[0])
        except (UnicodeDecodeError, ValueError):
            return False
        if not isinstance(event, dict):
            return False
        self._take(event.get("usage"))
        message = event.get("message")
        if isinstance(message, dict):
            self._take(message.get("usage"))
        return (
            self.drop_usage_only
            and event.get("choices") == []
            and isinstance(event.get("usage"), dict)
        )

    def feed(self, chunk: bytes) -> bytes:
        """Return the bytes to send now. Complete events only, in order."""
        self._buffer += chunk.replace(b"\r\n", b"\n")
        out: list[bytes] = []
        while True:
            end = self._buffer.find(b"\n\n")
            if end < 0:
                break
            raw, self._buffer = self._buffer[: end + 2], self._buffer[end + 2 :]
            if not self._event(raw):
                out.append(raw)
        if len(self._buffer) > _MAX_BUFFER:
            out.append(self._buffer)
            self._buffer = b""
        return b"".join(out)

    def finish(self) -> bytes:
        """Flush what is left and report the counts, if the stream carried any."""
        rest = b""
        if self._buffer:
            if not self._event(self._buffer):
                rest = self._buffer
            self._buffer = b""
        if self.seen:
            note_tokens(prompt=self.prompt, completion=self.completion)
        return rest


def client_wants_usage(payload: dict[str, Any]) -> bool:
    options = payload.get("stream_options")
    return isinstance(options, dict) and bool(options.get("include_usage"))


def with_stream_usage(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload with ``include_usage`` asked for, without touching the caller's dict."""
    options = payload.get("stream_options")
    merged = {**(options if isinstance(options, dict) else {}), "include_usage": True}
    return {**payload, "stream_options": merged}
