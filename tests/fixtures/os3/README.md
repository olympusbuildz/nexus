# OS3 replay fixtures

Three request bodies in the shape OS3's "local model" setting sends to
`POST /v1/chat/completions`:

| File | Role | What it carries |
| --- | --- | --- |
| `chat.json` | main chat | orchestrator tools (`create_task`, `notify_before_act`, ...), a `<nodes>` device list |
| `worker.json` | worker | "You are a worker agent" prompt, shell/file tools, a multi-turn history with tool results as text parts, nested tool schemas |
| `computer_use.json` | worker + screenshot | `computer_use` / `feed_image`, a prior capture, and the user turn `Image output of tool call <id>:` + `image_url` data URL |

## Provenance, and what is not real

The shapes (tool names and argument names, the `<node id=".." name="..">` list with
`<hostname>`, tool results as `[{"type":"text"}]` parts, screenshots re-sent as a
user `image_url` message, `reasoning_effort`) were read from
[Nesbesss/os3-router](https://github.com/Nesbesss/os3-router) (MIT): its tests, its
`bench/bench.py` (which plays OS3's worker loop), and `codex_os3/roles.py`. Nothing
is copied from it.

Rabbit's own tool descriptions and system prompts are not public, so **every string
here is synthetic and every schema is a reconstruction**. Device ids are made up, the
PNG is one pixel. These fixtures prove the bridge handles this *shape* of traffic.
They do not prove it matches what a given OS3 build sends: that needs a capture from
a real OS3 (see the live-test list in the PR).

## Role detection

`os3_bridge/roles.py` labels these three bodies chat, worker, and worker. The chat and
worker shapes match what os3-router classifies on. The background and image-check
shapes are **not** fixtures: they are synthetic tests in `tests/test_roles.py`, because
no real capture of them exists yet.
