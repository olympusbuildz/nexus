"""A built UI is served at / from the same origin; without one the dashboard is as it was."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nexus.app import create_app
from nexus.dashboard import DASHBOARD_HTML

from tests.test_v1_unchanged import GOLDEN, capture

INDEX = "<!doctype html><title>Nexus</title><div id=root></div>"

SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "site" / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "app-1a2b3c.js").write_text("export const x = 1;\n", encoding="utf-8")
    (root / "assets" / "app-1a2b3c.css").write_text("body{margin:0}\n", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (root / "v1").mkdir()
    (root / "v1" / "models").write_text("shadow", encoding="utf-8")
    (tmp_path / "site" / "secret.txt").write_text("outside the dist dir", encoding="utf-8")
    monkeypatch.setenv("OS3_UI_DIST", str(root))
    return root


def test_built_ui_is_served_at_root(dist: Path) -> None:
    with TestClient(create_app()) as client:
        page = client.get("/")
    assert page.status_code == 200
    assert page.text == INDEX
    assert "text/html" in page.headers["content-type"]
    assert page.headers["cache-control"] == "no-cache"
    assert page.headers["x-content-type-options"] == "nosniff"


def test_static_assets_are_served_with_their_types(dist: Path) -> None:
    with TestClient(create_app()) as client:
        script = client.get("/assets/app-1a2b3c.js")
        style = client.get("/assets/app-1a2b3c.css")
        icon = client.get("/favicon.svg")
        head = client.head("/assets/app-1a2b3c.js")
    assert script.text == "export const x = 1;\n"
    assert "javascript" in script.headers["content-type"]
    assert style.headers["content-type"].startswith("text/css")
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert head.status_code == 200
    assert head.content == b""


def test_client_side_routes_fall_back_to_the_app_shell(dist: Path) -> None:
    with TestClient(create_app()) as client:
        routes = [client.get(path) for path in ("/models", "/backends/codex", "/settings/keys/")]
    assert [r.status_code for r in routes] == [200, 200, 200]
    assert all(r.text == INDEX for r in routes)


def test_a_missing_asset_is_a_404_not_the_app_shell(dist: Path) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/assets/missing.js")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_api_paths_are_never_shadowed_by_the_ui(dist: Path, bridge_key: str) -> None:
    with TestClient(create_app()) as client:
        health = client.get("/health")
        doctor = client.get("/doctor")
        pulse = client.get("/dashboard/pulse")
        config = client.get("/dashboard/config")
        models = client.get("/dashboard/models")
        key = client.get("/dashboard/bridge-key", headers=SAME_ORIGIN)
        font = client.get("/dashboard/fonts/syne.woff2")
        v1 = client.get("/v1/models")
        v1_auth = client.get("/v1/models", headers={"Authorization": f"Bearer {bridge_key}"})
        unknown = client.get("/dashboard/nope")
    assert health.json()["ok"] is True
    assert doctor.json()["ok"] is True
    assert "entries" in pulse.json()
    assert config.json()["ok"] is True
    assert models.json()["backend"] == "openrouter"
    assert key.json()["present"] is True
    assert font.status_code == 200
    assert v1.status_code == 401
    assert v1_auth.status_code == 200
    assert v1_auth.json()["object"] == "list"
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Not Found"}


def test_only_get_and_head_reach_the_ui(dist: Path) -> None:
    with TestClient(create_app()) as client:
        post = client.post("/", json={})
        unknown_post = client.post("/models", json={})
    assert post.status_code == 405
    assert unknown_post.status_code == 404


def test_the_guard_still_runs_first(dist: Path) -> None:
    with TestClient(create_app()) as client:
        page = client.get("/", headers={"Host": "evil.example"})
        asset = client.get("/assets/app-1a2b3c.js", headers={"Host": "rebind.example:11435"})
        shell = client.get("/models", headers={"Host": "evil.example"})
    for response in (page, asset, shell):
        assert response.status_code == 403
        assert response.json()["code"] == "host_refused"
        assert "Nexus" not in response.text
        assert "export const" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/assets/../../secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "//secret.txt",
        "/assets/..%5c..%5csecret.txt",
        "/%00",
    ],
)
def test_nothing_outside_the_dist_directory_is_served(dist: Path, path: str) -> None:
    with TestClient(create_app()) as client:
        response = client.get(path)
    assert "outside the dist dir" not in response.text


def test_a_symlink_out_of_dist_is_not_followed(dist: Path) -> None:
    (dist / "assets" / "leak.txt").symlink_to(dist.parent / "secret.txt")
    with TestClient(create_app()) as client:
        response = client.get("/assets/leak.txt")
    assert "outside the dist dir" not in response.text


def test_without_a_built_ui_the_dashboard_is_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-dist"
    empty.mkdir()
    monkeypatch.setenv("OS3_UI_DIST", str(empty))
    with TestClient(create_app()) as client:
        page = client.get("/")
        unknown = client.get("/models")
        asset = client.get("/assets/app.js")
        post = client.post("/models", json={})
        wrong = client.put("/dashboard/config", json={})
    assert page.status_code == 200
    assert page.text == DASHBOARD_HTML
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Not Found"}
    assert asset.status_code == 404
    assert post.status_code == 404
    assert wrong.status_code == 405


def test_a_dist_directory_without_index_html_is_not_a_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "half-built"
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "app.js").write_text("x", encoding="utf-8")
    monkeypatch.setenv("OS3_UI_DIST", str(root))
    with TestClient(create_app()) as client:
        page = client.get("/")
        asset = client.get("/assets/app.js")
    assert page.text == DASHBOARD_HTML
    assert asset.status_code == 404


def test_default_location_is_ui_dist_under_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus import config
    from nexus.config import repo_root, ui_dist_dir

    monkeypatch.delenv("OS3_UI_DIST", raising=False)
    assert ui_dist_dir() == repo_root() / "ui" / "dist"
    monkeypatch.setattr(config, "repo_root", lambda: tmp_path)
    with TestClient(create_app()) as client:
        assert client.get("/").text == DASHBOARD_HTML


def test_the_ui_appearing_later_is_picked_up_without_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "later"
    monkeypatch.setenv("OS3_UI_DIST", str(root))
    with TestClient(create_app()) as client:
        assert client.get("/").text == DASHBOARD_HTML
        root.mkdir()
        (root / "index.html").write_text(INDEX, encoding="utf-8")
        assert client.get("/").text == INDEX


def test_v1_bytes_do_not_change_when_a_ui_is_served(
    dist: Path, bridge_key: str, openrouter_key: str
) -> None:
    import json

    got = capture(bridge_key)
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert got == expected
