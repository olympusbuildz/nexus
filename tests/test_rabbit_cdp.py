"""The CDP client against a fake Chrome (HTTP target API) and a fake page. No real Chrome."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexus import rabbit_cdp
from nexus.rabbit_cdp import (
    ORIGIN,
    SETTINGS_URL,
    ChromeCdp,
    ManualRequired,
    PageError,
    launch_argv,
    require_loopback_url,
)

KEY = "os3_FIXTURE_bridge_key_c1d7"
PORT = 9333
WS_URL = f"ws://127.0.0.1:{PORT}/devtools/page/T1"


class FakeChrome:
    """Chrome's HTTP target API, plus the page behind one ws connection."""

    def __init__(self, *, listening: bool = True) -> None:
        self.listening = listening
        self.target_url = SETTINGS_URL
        self.ws_url = WS_URL
        self.requests: list[tuple[str, str]] = []
        self.spawned: list[list[str]] = []
        self.window: dict[str, Any] = {}
        self.evaluated: list[str] = []
        self.page_text = "OS3 API keys"
        self.token_status = 200
        self.routes: dict[str, tuple[int, Any, bool]] = {
            "/api/byok/get": (200, {"credential": None}, True),
            "/api/byok/register": (200, {"ok": True}, True),
        }
        self.received: list[tuple[str, dict[str, Any], str | None]] = []
        self.eval_error: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, str(request.url)))
        if not self.listening:
            raise httpx.ConnectError("refused", request=request)
        if path == "/json/version":
            return httpx.Response(200, json={"Browser": "Chrome/150"})
        if path == "/json/new":
            return httpx.Response(
                200, json={"id": "T1", "url": "about:blank", "webSocketDebuggerUrl": self.ws_url}
            )
        if path == "/json/list":
            return httpx.Response(
                200,
                json=[
                    {"id": "OTHER", "url": "https://mail.example.com/inbox"},
                    {"id": "T1", "url": self.target_url},
                ],
            )
        if path.startswith("/json/close/"):
            return httpx.Response(200, text="Target is closing")
        return httpx.Response(404)

    def spawn(self, argv: list[str]) -> None:
        self.spawned.append(argv)
        self.listening = True

    @contextlib.asynccontextmanager
    async def connect(self, url: str) -> AsyncIterator[FakeConn]:
        yield FakeConn(self)

    def chrome(self, tmp_path: Path, **kwargs: Any) -> ChromeCdp:
        binary = tmp_path / "chrome"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        defaults: dict[str, Any] = {
            "port": PORT,
            "profile_dir": tmp_path / "profile",
            "chrome_bin": str(binary),
            "transport": httpx.MockTransport(self.handler),
            "connect": self.connect,
            "spawn": self.spawn,
            "launch_wait": 1.0,
        }
        return ChromeCdp(**{**defaults, **kwargs})

    def requests_to(self, fragment: str) -> list[tuple[str, str]]:
        return [r for r in self.requests if fragment in r[1]]


class FakeConn:
    def __init__(self, chrome: FakeChrome) -> None:
        self.chrome = chrome
        self.replies: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        expression = message["params"]["expression"]
        self.chrome.evaluated.append(expression)
        self.replies.append({"id": message["id"], "result": self._answer(expression)})

    async def recv(self) -> str:
        return json.dumps(self.replies.pop(0))

    def _answer(self, expression: str) -> dict[str, Any]:
        chrome = self.chrome
        if chrome.eval_error is not None and "/api/auth/token" in expression:
            return {"exceptionDetails": {"text": chrome.eval_error}}
        on_origin = chrome.target_url.startswith(ORIGIN)
        if "document.readyState" in expression:
            return {"result": {"value": "complete" if on_origin else "wrong-origin"}}
        if "innerText" in expression:
            return {"result": {"value": chrome.page_text}}
        set_key = re.search(r"window\.__nexusKey = (\".*?\"), true\)", expression)
        if set_key:
            chrome.window["__nexusKey"] = json.loads(set_key.group(1))
            return {"result": {"value": True}}
        if "delete window.__nexusKey, true" in expression:
            chrome.window.pop("__nexusKey", None)
            return {"result": {"value": True}}
        if "/api/auth/token" in expression:
            return {"result": {"value": self._call(expression, on_origin)}}
        return {"result": {"value": None}}

    def _call(self, expression: str, on_origin: bool) -> dict[str, Any]:
        chrome = self.chrome
        if not on_origin:
            return {"guard": "https://elsewhere.example"}
        key = None
        if "const key = window.__nexusKey" in expression:
            key = chrome.window.pop("__nexusKey", None)
        if chrome.token_status != 200:
            return {"auth_status": chrome.token_status}
        path = json.loads(re.search(r"fetch\((\"/api/[^\"]+\")", expression).group(1))
        body = json.loads(re.search(r"const body = (.*);\n", expression).group(1))
        chrome.received.append((path, body, key))
        status, data, is_json = chrome.routes.get(path, (404, None, True))
        return {"status": status, "data": data, "json": is_json}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _use(chrome: ChromeCdp, work: Any) -> Any:
    async with chrome.open_page() as page:
        return await work(page)


@pytest.mark.parametrize("host", ["10.0.0.5", "0.0.0.0", "localhost", "example.com", "::1", ""])
def test_non_loopback_cdp_host_is_refused_without_any_request(host: str, tmp_path: Path) -> None:
    fake = FakeChrome()
    chrome = fake.chrome(tmp_path, host=host)
    with pytest.raises(ManualRequired) as raised:
        run(_use(chrome, lambda page: page.call("/api/byok/get", {})))
    assert raised.value.code == "cdp_not_loopback"
    with pytest.raises(ManualRequired):
        run(chrome.listening())
    assert fake.requests == [] and fake.spawned == []


@pytest.mark.parametrize(
    "url",
    [
        "ws://192.168.1.9:9333/devtools/page/T1",
        "ws://127.0.0.1:9444/devtools/page/T1",
        "ws://localhost:9333/devtools/page/T1",
        "wss://127.0.0.1:9333/devtools/page/T1",
        "ws://127.0.0.1.evil.example:9333/devtools/page/T1",
    ],
)
def test_debugger_url_must_name_this_loopback_port(url: str, tmp_path: Path) -> None:
    with pytest.raises(ManualRequired) as raised:
        require_loopback_url(url, PORT)
    assert raised.value.code == "cdp_not_loopback"

    fake = FakeChrome()
    fake.ws_url = url
    with pytest.raises(ManualRequired):
        run(_use(fake.chrome(tmp_path), lambda page: page.call("/api/byok/get", {})))
    assert fake.evaluated == []
    assert fake.requests_to("/json/close/T1"), "the target we opened is closed even on refusal"


@pytest.mark.parametrize(
    "url",
    [
        f"ws://127.0.0.1:{PORT}/devtools/page/OTHER",
        f"ws://127.0.0.1:{PORT}/devtools/browser/T1-extra",
        f"ws://127.0.0.1:{PORT}/devtools/page/T1/more",
        f"ws://127.0.0.1:{PORT}/",
    ],
)
def test_debugger_url_must_be_for_the_tab_we_opened(url: str, tmp_path: Path) -> None:
    fake = FakeChrome()
    fake.ws_url = url
    with pytest.raises(ManualRequired) as raised:
        run(_use(fake.chrome(tmp_path), lambda page: page.call("/api/byok/get", {})))
    assert raised.value.code == "cdp_unavailable"
    assert fake.evaluated == []
    assert fake.requests_to("/json/close/T1"), "the target we opened is closed even on refusal"


def test_debugger_url_on_loopback_is_accepted() -> None:
    require_loopback_url(WS_URL, PORT)


def test_launches_headed_chrome_on_the_dedicated_profile_when_nothing_listens(
    tmp_path: Path,
) -> None:
    fake = FakeChrome(listening=False)
    run(_use(fake.chrome(tmp_path), lambda page: page.call("/api/byok/get", {})))

    assert len(fake.spawned) == 1
    argv = fake.spawned[0]
    assert f"--remote-debugging-port={PORT}" in argv
    assert "--remote-debugging-address=127.0.0.1" in argv
    assert f"--user-data-dir={tmp_path / 'profile'}" in argv
    assert not any("headless" in item.lower() for item in argv)
    assert not any("user-agent" in item.lower() for item in argv)
    assert (tmp_path / "profile").is_dir()


def test_attaches_without_launching_when_chrome_already_listens(tmp_path: Path) -> None:
    fake = FakeChrome()
    run(_use(fake.chrome(tmp_path), lambda page: page.call("/api/byok/get", {})))
    assert fake.spawned == []


def test_launch_argv_is_never_headless_and_never_spoofs() -> None:
    argv = launch_argv("/chrome", port=1234, profile=Path("/p"))
    assert argv[0] == "/chrome"
    assert not any("headless" in a or "user-agent" in a or "disable-web" in a for a in argv)
    source = Path(rabbit_cdp.__file__).read_text()
    assert "--headless" not in source and "--user-agent" not in source


def test_no_chrome_installed_is_manual(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rabbit_cdp.shutil, "which", lambda name: None)
    monkeypatch.setattr(rabbit_cdp.sys, "platform", "linux")
    fake = FakeChrome(listening=False)
    chrome = fake.chrome(tmp_path, chrome_bin=None)
    with pytest.raises(ManualRequired) as raised:
        run(_use(chrome, lambda page: page.call("/api/byok/get", {})))
    assert raised.value.code == "chrome_unavailable" and fake.spawned == []


def test_chrome_that_never_answers_is_manual(tmp_path: Path) -> None:
    fake = FakeChrome(listening=False)
    fake.spawn = lambda argv: fake.spawned.append(argv)  # type: ignore[method-assign]
    chrome = fake.chrome(tmp_path, spawn=fake.spawn, launch_wait=0.3)
    with pytest.raises(ManualRequired) as raised:
        run(_use(chrome, lambda page: page.call("/api/byok/get", {})))
    assert raised.value.code == "cdp_unavailable"
    assert "normal Chrome window" in raised.value.message


def test_spawn_failure_is_manual(tmp_path: Path) -> None:
    def broken(argv: list[str]) -> None:
        raise OSError(13, "Permission denied")

    fake = FakeChrome(listening=False)
    with pytest.raises(ManualRequired) as raised:
        run(_use(fake.chrome(tmp_path, spawn=broken), lambda page: page.call("x", {})))
    assert raised.value.code == "chrome_unavailable"


def test_opens_one_target_uses_only_it_and_closes_it(tmp_path: Path) -> None:
    fake = FakeChrome()
    run(_use(fake.chrome(tmp_path), lambda page: page.call("/api/byok/get", {"feature": "os3"})))

    opened = [r for r in fake.requests if "/json/new" in r[1]]
    assert len(opened) == 1 and opened[0][0] == "PUT"
    assert SETTINGS_URL in opened[0][1]
    closed = [r for r in fake.requests if "/json/close/" in r[1]]
    assert [r[1].rsplit("/", 1)[1] for r in closed] == ["T1"]
    assert not [r for r in fake.requests if "OTHER" in r[1]]


def test_target_is_closed_when_the_work_fails(tmp_path: Path) -> None:
    fake = FakeChrome()

    async def boom(page: Any) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run(_use(fake.chrome(tmp_path), boom))
    assert len(fake.requests_to("/json/close/T1")) == 1


def test_nothing_is_evaluated_unless_the_origin_is_os3(tmp_path: Path) -> None:
    fake = FakeChrome()
    fake.target_url = "https://accounts.example.com/login?next=os3"
    with pytest.raises(ManualRequired) as raised:
        run(_use(fake.chrome(tmp_path), lambda page: page.call("/api/byok/get", {})))
    assert raised.value.code == "signed_out"
    assert fake.evaluated == []
    assert fake.received == []


def test_look_alike_origins_do_not_count(tmp_path: Path) -> None:
    for url in (
        "https://os3.rabbit.tech.evil.example/settings",
        "http://os3.rabbit.tech/settings",
        "https://os3.rabbit.tech:8443/settings",
        "https://evil.example/https://os3.rabbit.tech",
    ):
        fake = FakeChrome()
        fake.target_url = url
        with pytest.raises(ManualRequired):
            run(_use(fake.chrome(tmp_path), lambda page: page.call("/api/byok/get", {})))
        assert fake.evaluated == [], url


def test_a_tab_that_moves_off_origin_after_ready_is_not_used(tmp_path: Path) -> None:
    fake = FakeChrome()

    async def work(page: Any) -> None:
        before = len(fake.evaluated)
        fake.target_url = "https://evil.example/"
        with pytest.raises(ManualRequired) as raised:
            await page.call("/api/byok/get", {})
        assert raised.value.code == "wrong_origin"
        assert len(fake.evaluated) == before
        with pytest.raises(ManualRequired):
            await page.register({"provider": "local"}, KEY)
        assert all(KEY not in expression for expression in fake.evaluated)

    run(_use(fake.chrome(tmp_path), work))


def test_every_page_expression_checks_the_origin_itself() -> None:
    for script in (
        rabbit_cdp._call_script("/api/byok/get", {}, key_from_window=False),
        rabbit_cdp._call_script("/api/byok/register", {}, key_from_window=True),
        rabbit_cdp._SET_KEY,
        rabbit_cdp._CLEAR_KEY,
        rabbit_cdp._READY,
        rabbit_cdp._PAGE_TEXT,
    ):
        assert "location.origin" in script and ORIGIN in script


def test_an_error_page_is_manual(tmp_path: Path) -> None:
    fake = FakeChrome()
    fake.target_url = "chrome-error://chromewebdata/"
    with pytest.raises(ManualRequired) as raised:
        run(_use(fake.chrome(tmp_path), lambda page: page.call("x", {})))
    assert raised.value.code == "page_failed" and fake.evaluated == []


def test_cloudfront_block_page_is_manual_and_not_retried(tmp_path: Path) -> None:
    fake = FakeChrome()
    fake.page_text = "ERROR 403 ERROR The request could not be satisfied. Generated by CloudFront"
    with pytest.raises(ManualRequired) as raised:
        run(_use(fake.chrome(tmp_path), lambda page: page.call("x", {})))
    assert raised.value.code == "blocked"
    assert not fake.requests_to("/json/new?") or len(fake.requests_to("/json/new?")) == 1
    assert fake.received == []


@pytest.mark.parametrize(("status", "code"), [(401, "signed_out"), (403, "blocked")])
def test_token_failure_is_signed_out_or_blocked(status: int, code: str, tmp_path: Path) -> None:
    fake = FakeChrome()
    fake.token_status = status
    with pytest.raises(ManualRequired) as raised:
        run(_use(fake.chrome(tmp_path), lambda page: page.call("/api/byok/get", {})))
    assert raised.value.code == code
    assert fake.received == []


def test_a_non_json_403_from_an_api_is_a_block_but_a_json_403_is_an_answer(
    tmp_path: Path,
) -> None:
    fake = FakeChrome()
    fake.routes["/api/byok/get"] = (403, None, False)
    fake.routes["/api/byok/delete"] = (403, {"error": "not yours"}, True)

    async def work(page: Any) -> Any:
        with pytest.raises(ManualRequired) as raised:
            await page.call("/api/byok/get", {})
        assert raised.value.code == "blocked"
        return await page.call("/api/byok/delete", {})

    reply = run(_use(fake.chrome(tmp_path), work))
    assert reply.status == 403 and reply.data == {"error": "not yours"}


def test_a_page_exception_is_a_page_error(tmp_path: Path) -> None:
    fake = FakeChrome()
    fake.eval_error = "Uncaught TypeError"
    with pytest.raises(PageError, match="TypeError"):
        run(_use(fake.chrome(tmp_path), lambda page: page.call("/api/byok/get", {})))


def test_key_is_placed_for_the_register_call_only_and_removed(tmp_path: Path) -> None:
    fake = FakeChrome()
    body = {"provider": "local", "feature": "os3", "nodeId": "n1"}

    async def work(page: Any) -> Any:
        reply = await page.register(body, KEY)
        assert fake.window == {}
        return reply

    reply = run(_use(fake.chrome(tmp_path), work))
    assert reply.ok and reply.data == {"ok": True}
    assert fake.received == [("/api/byok/register", body, KEY)]
    holders = [e for e in fake.evaluated if KEY in e]
    assert len(holders) == 1 and "window.__nexusKey = " in holders[0]
    assert fake.window == {}
    call = next(e for e in fake.evaluated if "/api/byok/register" in e)
    assert "const key = window.__nexusKey; delete window.__nexusKey;" in call
    assert call.index("delete window.__nexusKey") < call.index("await")


def test_key_is_removed_even_when_the_call_fails(tmp_path: Path) -> None:
    fake = FakeChrome()
    fake.routes["/api/byok/register"] = (500, {"error": "no"}, True)

    async def work(page: Any) -> None:
        reply = await page.register({"provider": "local"}, KEY)
        assert reply.status == 500 and fake.window == {}
        fake.token_status = 401
        with pytest.raises(ManualRequired):
            await page.register({"provider": "local"}, KEY)
        assert fake.window == {}

    run(_use(fake.chrome(tmp_path), work))


def test_key_is_scrubbed_from_page_errors(tmp_path: Path) -> None:
    fake = FakeChrome()
    fake.eval_error = f"Uncaught Error: bad key {KEY}"

    chrome = fake.chrome(tmp_path, scrub=lambda text: text.replace(KEY, "[REDACTED]"))
    with pytest.raises(PageError) as raised:
        run(_use(chrome, lambda page: page.call("/api/byok/get", {})))
    assert KEY not in str(raised.value) and "[REDACTED]" in str(raised.value)


def test_the_bearer_token_is_never_returned_by_the_page_script() -> None:
    script = rabbit_cdp._call_script("/api/byok/get", {}, key_from_window=False)
    returns = re.findall(r"return \{[^}]*\}", script)
    assert returns and not any(
        "token" in r.replace("auth_status", "").replace("no_token", "") for r in returns
    )


def test_default_connect_builds_a_websocket_client_without_a_new_dependency() -> None:
    assert rabbit_cdp._default_connect(WS_URL) is not None
