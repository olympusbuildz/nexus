"""The unauthenticated routes must refuse a browser acting for a foreign page."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from nexus.app import create_app

GOOD = {"backend": "openai", "api_key": "sk-proj-guard-test-key"}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    choice = tmp_path / "choice"
    key = tmp_path / "openai.key"
    monkeypatch.setenv("OS3_BRIDGE_BACKEND_FILE", str(choice))
    monkeypatch.setenv("OS3_BRIDGE_OPENAI_KEY_FILE", str(key))
    return choice, key


def _untouched(paths) -> bool:
    return not any(p.exists() for p in paths)


def test_cross_origin_text_plain_write_is_refused(paths) -> None:
    """The no-preflight request a hostile page can send to 127.0.0.1."""
    with TestClient(create_app()) as client:
        response = client.post(
            "/dashboard/config",
            content='{"backend":"openai","api_key":"sk-attacker-key"}',
            headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
        )
    assert response.status_code == 403
    assert _untouched(paths)


def test_cross_origin_json_write_is_refused(paths) -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/dashboard/config", json=GOOD, headers={"Origin": "https://evil.example"}
        )
    assert response.status_code == 403
    assert _untouched(paths)


def test_cross_site_fetch_metadata_is_refused(paths) -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/dashboard/config", json=GOOD, headers={"Sec-Fetch-Site": "cross-site"}
        )
    assert response.status_code == 403
    assert _untouched(paths)


def test_write_needs_json_even_without_an_origin(paths) -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/dashboard/config", content="{}", headers={"Content-Type": "text/plain"}
        )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "path", ["/", "/health", "/doctor", "/dashboard/pulse", "/dashboard/bridge-key"]
)
def test_foreign_host_is_refused_for_dns_rebinding(path: str) -> None:
    with TestClient(create_app()) as client:
        response = client.get(path, headers={"Host": "attacker.example:11435"})
    assert response.status_code == 403


def test_foreign_host_cannot_write(paths) -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/dashboard/config",
            json=GOOD,
            headers={"Host": "attacker.example:11435", "Origin": "http://attacker.example:11435"},
        )
    assert response.status_code == 403
    assert _untouched(paths)


@pytest.mark.parametrize("host", ["127.0.0.1:11435", "localhost:11435", "[::1]:11435"])
def test_loopback_hosts_are_served(host: str) -> None:
    with TestClient(create_app()) as client:
        assert client.get("/health", headers={"Host": host}).status_code == 200


def test_same_origin_dashboard_write_still_works(paths) -> None:
    choice, key = paths
    with TestClient(create_app()) as client:
        response = client.post(
            "/dashboard/config",
            json=GOOD,
            headers={
                "Origin": "http://127.0.0.1:11435",
                "Sec-Fetch-Site": "same-origin",
            },
        )
    assert response.status_code == 200
    assert choice.read_text(encoding="utf-8").strip() == "openai"
    assert key.exists()


def test_v1_clients_are_not_subject_to_the_browser_guard(auth_header) -> None:
    """SDKs and curl send no Origin; /v1 stays bearer-only."""
    with TestClient(create_app()) as client:
        response = client.get("/v1/nope", headers=auth_header)
    assert response.status_code == 404


BRIDGE_KEY_REFUSED = {
    "cross-site fetch": ({"Sec-Fetch-Site": "cross-site"}, "cross_site_refused"),
    "same-site fetch": ({"Sec-Fetch-Site": "same-site"}, "cross_site_refused"),
    "address-bar navigation": ({"Sec-Fetch-Site": "none"}, "provenance_required"),
    "no provenance at all (curl, scripts)": ({}, "provenance_required"),
    "foreign origin": ({"Origin": "https://evil.example"}, "origin_mismatch"),
    "foreign origin on a loopback port": ({"Origin": "http://localhost:3000"}, "origin_mismatch"),
    "null origin": ({"Origin": "null"}, "origin_mismatch"),
    "origin names localhost, host is 127.0.0.1": (
        {"Origin": "http://localhost:11435"},
        "origin_mismatch",
    ),
    "origin and host agree on the wrong port": (
        {"Origin": "http://127.0.0.1:9999", "Host": "127.0.0.1:9999"},
        "provenance_required",
    ),
    "origin with the wrong scheme": (
        {"Origin": "https://127.0.0.1:11435"},
        "provenance_required",
    ),
    "foreign host": ({"Host": "attacker.example"}, "host_refused"),
    "foreign host with a loopback port": ({"Host": "attacker.example:11435"}, "host_refused"),
    "foreign host claiming same-origin": (
        {"Host": "attacker.example", "Sec-Fetch-Site": "same-origin"},
        "host_refused",
    ),
    "foreign host with an origin that matches it": (
        {"Host": "attacker.example", "Origin": "http://attacker.example"},
        "host_refused",
    ),
}


@pytest.mark.parametrize(
    ("headers", "code"), BRIDGE_KEY_REFUSED.values(), ids=BRIDGE_KEY_REFUSED.keys()
)
def test_bridge_key_is_refused_without_same_origin_provenance(
    headers: dict[str, str], code: str, bridge_key: str
) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/dashboard/bridge-key", headers=headers)
    body = response.json()
    error = body["error"]
    assert response.status_code == 403
    assert bridge_key not in response.text
    assert (error["code"] if isinstance(error, dict) else body["code"]) == code
    assert (error if isinstance(error, dict) else body)["hint"]
    if code == "provenance_required":
        assert error["type"] == "permission_error"
        assert error["key_file"].endswith("bridge.key")


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1:11435"},
        {"Origin": "http://127.0.0.1:11435"},
        {"Sec-Fetch-Site": "none", "Origin": "http://127.0.0.1:11435"},
    ],
)
def test_bridge_key_is_served_to_its_own_page(headers: dict[str, str], bridge_key: str) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/dashboard/bridge-key", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"present": True, "key": bridge_key}


def test_port_follows_nexus_port(monkeypatch: pytest.MonkeyPatch, bridge_key: str) -> None:
    monkeypatch.setenv("NEXUS_PORT", "11500")
    with TestClient(create_app(), base_url="http://127.0.0.1:11500") as client:
        ok = client.get("/dashboard/bridge-key", headers={"Origin": "http://127.0.0.1:11500"})
        old = client.get("/dashboard/bridge-key", headers={"Origin": "http://127.0.0.1:11435"})
    assert ok.status_code == 200
    assert old.status_code == 403


def test_other_gets_still_ignore_sec_fetch_site() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 200


def test_security_policy_names_the_rules_the_guard_enforces() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "SECURITY.md").read_text(encoding="utf-8")
    for needle in (
        "127.0.0.1:11435",
        "Host",
        "application/json",
        "Sec-Fetch-Site",
        "advisories/new",
    ):
        assert needle in text


def test_only_the_dashboard_pages_fetch_the_bridge_key() -> None:
    """Non-browser tools (CLI, install, doctor, rabbit setup) read the key file, not the route."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    allowed = {
        "src/nexus/app.py",
        "src/nexus/guard.py",
        "src/nexus/dashboard.py",
        "ui/src/api/live.ts",
    }
    callers = set()
    for path in [
        *root.glob("src/**/*.py"),
        *root.glob("*.sh"),
        *root.glob("scripts/**/*"),
        root / "nexus",
        root / "os3-bridge",
        *root.glob("ui/src/**/*.ts"),
    ]:
        if path.is_file() and not path.name.endswith((".test.ts", ".test.tsx")):
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "/dashboard/bridge-key" in text:
                callers.add(str(path.relative_to(root)))
    assert callers <= allowed, callers - allowed
    from nexus import rabbit_setup

    assert "/dashboard/bridge-key" not in Path(rabbit_setup.__file__).read_text(encoding="utf-8")
    assert "read_bridge_key" in Path(rabbit_setup.__file__).read_text(encoding="utf-8")
