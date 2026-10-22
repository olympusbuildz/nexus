"""Auto-detecting the restore key file from OS3's masked key. No real keys, no network."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from nexus import cli, rabbit_cli, rabbit_keys
from nexus.config import rabbit_key_dirs
from nexus.rabbit_keys import find_restore_key, masked_key_matches, scan_key_files
from nexus.rabbit_setup import RabbitSetup

from tests.rabbit_fakes import BRIDGE_KEY, RESTORE_KEY, FakeBrowser, FakeOs, fixture, models_source
from tests.test_rabbit_dashboard import Harness, post
from tests.test_rabbit_setup import (  # noqa: F401
    backup_text,
    engine,
    rabbit_env,
    run,
)

OTHER_KEY = "sk-or-v1-EXAMPLE-other-key-0000"
SAME_MASK_KEY = "sk-or-v1-FIXTURE-twin-key-wxyz"
MASKED = "sk-or-v1-****wxyz"
MODEL = "openai/gpt-4o-mini"


def write_key(directory: Path, name: str, value: str, mode: int = 0o600) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture
def base(rabbit_env: Path) -> Path:  # noqa: F811
    return rabbit_env


@pytest.fixture
def keys(base: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = base / "saved-keys"
    directory.mkdir()
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_KEY_DIRS", str(directory))
    return directory


def openrouter_page() -> FakeOs:
    return FakeOs(fixture("byok_get_custom_openrouter.json"))


@pytest.fixture
def os_page(base: Path, monkeypatch: pytest.MonkeyPatch) -> FakeOs:
    page = openrouter_page()
    browser = FakeBrowser(page)

    def build(**kwargs: Any) -> RabbitSetup:
        return RabbitSetup(browser=browser, models_source=models_source, retry_delay=0, **kwargs)

    monkeypatch.setattr(rabbit_cli, "RabbitSetup", build)
    return page


def run_cli(*argv: str) -> int:
    return cli.main(["rabbit", *argv])


def not_restorable(result: dict[str, Any]) -> bool:
    return any(
        "NEXUS_RABBIT_RESTORE_KEY_FILE if you want it restorable" in w for w in result["warnings"]
    )


@pytest.mark.parametrize(
    ("masked", "key", "expected"),
    [
        (MASKED, RESTORE_KEY, True),
        (MASKED, "sk-or-v1-x-wxyz", True),
        ("sk-****wxyz", "sk-anything-at-all-wxyz", True),
        ("****wxyz", "anything-at-all-wxyz", False),
        ("sk****wxyz", "sk-anything-at-all-wxyz", False),
        ("sk-or-v1-****xyz", RESTORE_KEY, False),
        ("****ab", "sk-or-v1-FIXTURE-key-ab", False),
        ("****b", "sk-or-v1-FIXTURE-key-ab", False),
        (MASKED, "sk-or-v1-FIXTURE-restore-key-wxyy", False),
        (MASKED, "sk-ant-FIXTURE-restore-key-wxyz", False),
        (MASKED, "sk-or-v1-wxyz", False),
        (MASKED, "", False),
        ("sk-or-v1-****", RESTORE_KEY, False),
        ("no mask here wxyz", "no mask here wxyz", False),
        (None, RESTORE_KEY, False),
    ],
)
def test_mask_needs_three_prefix_and_four_suffix_characters_that_both_fit(
    masked: str | None, key: str, expected: bool
) -> None:
    assert masked_key_matches(masked, key) is expected


def test_default_dir_is_only_the_rabbit_keys_folder_next_to_the_bridge_key(base: Path) -> None:
    assert rabbit_key_dirs() == [(base / "rabbit-keys").resolve()]


def test_env_dirs_come_first_split_on_pathsep_and_are_not_repeated(
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one, two = base / "one", base / "two"
    monkeypatch.setenv(
        "OS3_BRIDGE_RABBIT_KEY_DIRS",
        os.pathsep.join([str(one), "", str(two), str(one), str(base / "rabbit-keys")]),
    )
    assert rabbit_key_dirs() == [one.resolve(), two.resolve(), (base / "rabbit-keys").resolve()]


def test_a_key_in_the_default_rabbit_keys_folder_is_found(base: Path) -> None:
    path = write_key(base / "rabbit-keys", "openrouter", RESTORE_KEY)
    found = find_restore_key(MASKED)
    assert found.source == "auto-detected" and found.path == path.resolve()


def test_the_bridge_config_dir_itself_is_not_searched_or_warned_about(base: Path) -> None:
    write_key(base, "stray.key", RESTORE_KEY)
    write_key(base, "README.md", "# ordinary file", mode=0o644)

    found = find_restore_key(MASKED)

    assert found.source == "none" and found.unreadable == []
    assert found.warnings() == []


def test_the_bridge_key_is_never_a_candidate_even_in_a_configured_dir(
    keys: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = write_key(keys, "bridge.key", RESTORE_KEY)
    monkeypatch.setenv("OS3_BRIDGE_KEY_FILE", str(own))
    (keys / "alias").symlink_to("bridge.key")
    os.link(own, keys / "hardlink")
    other = write_key(keys, "other.key", OTHER_KEY)

    scan = scan_key_files(MASKED, [keys])

    assert scan.matches == [] and scan.candidates == 1
    assert find_restore_key(MASKED).source == "none"
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_KEY_DIRS", str(keys))
    assert find_restore_key("sk-or-v1-****0000").path == other.resolve()


def test_a_configured_dir_that_holds_the_bridge_key_still_finds_the_other_files(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_KEY_DIRS", str(base))
    path = write_key(base, "openrouter.key", RESTORE_KEY)

    found = find_restore_key(MASKED)

    assert found.path == path.resolve()
    assert scan_key_files(None, [base]).candidates == 1


def test_gitignore_ignores_the_default_rabbit_keys_folder() -> None:
    root = Path(__file__).resolve().parent.parent
    target = "rabbit-keys/openrouter-key"
    if shutil.which("git") and (root / ".git").exists():
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", target], cwd=root, check=False
        ).returncode
        assert ignored == 0, f"{target} is not ignored by git"
    else:
        lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert "rabbit-keys/" in [line.strip() for line in lines]


def test_only_small_regular_files_directly_in_the_dir_are_candidates(keys: Path) -> None:
    good = write_key(keys, "good", RESTORE_KEY)
    (keys / "big").write_text(RESTORE_KEY + " " * 5000)
    (keys / "big").chmod(0o600)
    edge = keys / "edge"
    edge.write_text(RESTORE_KEY + " " * (4096 - len(RESTORE_KEY)))
    edge.chmod(0o600)
    write_key(keys / "nested", "deep", RESTORE_KEY)
    os.mkfifo(keys / "fifo")

    scan = scan_key_files(MASKED, [keys])

    assert sorted(scan.matches) == sorted([keys.resolve() / "edge", good.resolve()])
    assert scan.candidates == 2 and scan.unreadable == []


def test_a_file_must_hold_exactly_one_key_on_one_line(keys: Path) -> None:
    padded = write_key(keys, "padded", f"  {RESTORE_KEY}  \n")
    commented = write_key(keys, "commented", f"{RESTORE_KEY}\n# saved from openrouter dashboard")
    second = write_key(keys, "second", f"# comment\n{RESTORE_KEY}")
    spaced = write_key(keys, "spaced", f"key: {RESTORE_KEY}")

    scan = scan_key_files(MASKED, [keys])

    assert scan.matches == [padded.resolve()]
    assert sorted(scan.rejected) == sorted(p.resolve() for p in (commented, second, spaced))
    assert rabbit_keys.read_key_file(padded) == RESTORE_KEY
    for path in (commented, second, spaced):
        with pytest.raises(rabbit_keys.KeyFileError, match="exactly one key"):
            rabbit_keys.read_key_file(path)


def test_a_symlink_is_never_followed_even_to_a_file_in_the_same_dir(keys: Path) -> None:
    real = write_key(keys, "real", RESTORE_KEY)
    (keys / "alias").symlink_to("real")

    found = find_restore_key(MASKED)

    assert found.source == "auto-detected" and found.path == real.resolve()
    assert scan_key_files(MASKED, [keys]).candidates == 1
    with pytest.raises(rabbit_keys.KeyFileError, match="symlink"):
        rabbit_keys.read_key_file(keys / "alias")


def test_a_symlink_that_points_outside_the_dir_is_skipped(
    keys: Path,
    base: Path,
) -> None:
    outside = write_key(base / "elsewhere", "outside", RESTORE_KEY)
    (keys / "link").symlink_to(outside)

    scan = scan_key_files(MASKED, [keys])

    assert scan.matches == [] and scan.candidates == 0


def test_a_symlink_to_a_nested_file_is_skipped(keys: Path) -> None:
    deep = write_key(keys / "nested", "deep", RESTORE_KEY)
    (keys / "link").symlink_to(deep)
    assert scan_key_files(MASKED, [keys]).matches == []


def test_unique_match_is_used_and_reported_in_dry_run(keys: Path) -> None:
    path = write_key(keys, "openrouter.key", RESTORE_KEY)
    write_key(keys, "unrelated.key", OTHER_KEY)
    rabbit, _, page = engine(openrouter_page())

    result = run(rabbit.apply(model=MODEL, dry_run=True))

    assert result["ok"] and result["dry_run"]
    assert result["restore_key"] == {"source": "auto-detected", "path": str(path.resolve())}
    assert not not_restorable(result)
    assert any("currently uses custom" in w and "replaced" in w for w in result["warnings"])
    assert page.paths == ["/api/byok/get"]


def test_unique_match_puts_the_provider_back_when_the_change_fails(keys: Path) -> None:
    write_key(keys, "openrouter.key", RESTORE_KEY)
    page = openrouter_page()
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)

    result = run(rabbit.apply(model=MODEL))

    assert result["code"] == "restore_attempted" and result["restore"]["outcome"] == "restored"
    assert page.state["provider"] == "custom"
    assert page.registered_keys == [BRIDGE_KEY, RESTORE_KEY]
    assert RESTORE_KEY not in json.dumps(result)


def test_no_match_keeps_the_not_restorable_warning(keys: Path) -> None:
    write_key(keys, "other.key", OTHER_KEY)
    page = openrouter_page()
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)

    dry = run(rabbit.apply(model=MODEL, dry_run=True))
    assert dry["restore_key"] == {"source": "none", "path": None}
    assert not_restorable(dry)

    result = run(rabbit.apply(model=MODEL))
    assert result["code"] == "restore_failed" and page.state is None
    assert "NEXUS_RABBIT_RESTORE_KEY_FILE" in result["restore"]["detail"]
    assert OTHER_KEY not in json.dumps(result)


def test_ambiguous_match_picks_nothing_and_lists_the_paths(keys: Path) -> None:
    first = write_key(keys, "a.key", RESTORE_KEY)
    second = write_key(keys, "b.key", SAME_MASK_KEY)
    page = openrouter_page()
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)

    dry = run(rabbit.apply(model=MODEL, dry_run=True))
    assert dry["restore_key"]["source"] == "ambiguous" and dry["restore_key"]["path"] is None
    assert dry["restore_key"]["paths"] == [str(first.resolve()), str(second.resolve())]
    text = " ".join(dry["warnings"])
    assert "none was picked" in text and str(first.resolve()) in text
    assert str(second.resolve()) in text
    assert RESTORE_KEY not in json.dumps(dry) and SAME_MASK_KEY not in json.dumps(dry)

    result = run(rabbit.apply(model=MODEL))
    assert result["code"] == "restore_failed" and page.state is None
    assert page.registered_keys == [BRIDGE_KEY]


def test_a_group_readable_file_is_skipped_unread_and_the_warning_says_so(
    keys: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loose = write_key(keys, "loose.key", RESTORE_KEY, mode=0o640)
    group_write = write_key(keys, "group-write.key", RESTORE_KEY, mode=0o620)
    world = write_key(keys, "world.key", RESTORE_KEY, mode=0o604)
    world_write = write_key(keys, "world-write.key", RESTORE_KEY, mode=0o602)
    rabbit, _, _ = engine(openrouter_page())

    result = run(rabbit.apply(model=MODEL, dry_run=True))

    assert result["restore_key"]["source"] == "none"
    assert result["restore_key"]["skipped_unreadable"] == [
        str(p.resolve()) for p in (group_write, loose, world_write, world)
    ]
    text = " ".join(result["warnings"])
    assert "not owner-only" in text and str(group_write.resolve()) in text
    for path in (loose, group_write, world, world_write):
        with pytest.raises(rabbit_keys.KeyFileError, match="owner-only"):
            rabbit_keys.read_key_file(path)
    assert not_restorable(result)


def test_fixing_the_mode_makes_the_skipped_file_a_match(keys: Path) -> None:
    path = write_key(keys, "loose.key", RESTORE_KEY, mode=0o644)
    assert find_restore_key(MASKED).source == "none"
    path.chmod(0o600)
    assert find_restore_key(MASKED).path == path.resolve()


def test_env_var_wins_and_the_dirs_are_not_searched(
    keys: Path,
    base: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    write_key(keys, "auto.key", SAME_MASK_KEY)
    explicit = write_key(base / "explicit", "env.key", RESTORE_KEY)
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(explicit))
    monkeypatch.setattr(
        rabbit_keys, "scan_key_files", lambda *a, **k: pytest.fail("env var must skip the scan")
    )
    page = openrouter_page()
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)

    dry = run(rabbit.apply(model=MODEL, dry_run=True))
    assert dry["restore_key"] == {"source": "env", "path": str(explicit)}

    result = run(rabbit.apply(model=MODEL))
    assert result["code"] == "restore_attempted"
    assert page.registered_keys == [BRIDGE_KEY, RESTORE_KEY]


def test_env_var_that_does_not_match_is_not_replaced_by_a_detected_file(
    keys: Path,
    base: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    write_key(keys, "auto.key", RESTORE_KEY)
    explicit = write_key(base / "explicit", "env.key", OTHER_KEY)
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(explicit))
    page = openrouter_page()
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)

    result = run(rabbit.apply(model=MODEL))

    assert result["code"] == "restore_failed"
    assert "does not match" in result["restore"]["detail"]
    assert page.registered_keys == [BRIDGE_KEY]


def test_restore_command_finds_the_key_on_its_own(keys: Path) -> None:
    write_key(keys, "openrouter.key", RESTORE_KEY)
    rabbit, _, page = engine(openrouter_page())
    assert run(rabbit.apply(model=MODEL))["ok"]

    result = run(rabbit.restore())

    assert result["ok"] and result["changed"]
    assert page.state["provider"] == "custom"
    assert page.registered_keys[-1] == RESTORE_KEY
    assert RESTORE_KEY not in json.dumps(result)


def test_a_local_connection_needs_no_scan(keys: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rabbit_keys, "scan_key_files", lambda *a, **k: pytest.fail("no scan for Local")
    )
    rabbit, _, _ = engine(FakeOs(fixture("byok_get_local_other.json")))
    result = run(rabbit.apply(model=MODEL, dry_run=True))
    assert "restore_key" not in result


def page_with_mask(masked: str) -> FakeOs:
    data = fixture("byok_get_custom_openrouter.json")
    data["credential"]["maskedKey"] = masked
    return FakeOs(data)


def test_a_key_with_a_comment_line_is_rejected_at_match_and_at_restore(keys: Path) -> None:
    path = write_key(keys, "openrouter.key", f"{RESTORE_KEY}\n# saved from openrouter dashboard")
    page = openrouter_page()
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)

    dry = run(rabbit.apply(model=MODEL, dry_run=True))
    assert dry["restore_key"]["source"] == "none"
    assert dry["restore_key"]["skipped_not_one_key"] == [str(path.resolve())]
    assert not_restorable(dry)
    assert any("exactly one key" in w and str(path.resolve()) in w for w in dry["warnings"])

    result = run(rabbit.apply(model=MODEL))
    assert result["code"] == "restore_failed" and page.state is None
    assert page.registered_keys == [BRIDGE_KEY]


def test_a_named_key_with_a_comment_line_is_rejected_at_dry_run_and_at_restore(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_key(base, "env.key", f"{RESTORE_KEY}\n# saved from openrouter dashboard")
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(path))
    page = openrouter_page()
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)

    dry = run(rabbit.apply(model=MODEL, dry_run=True))
    assert dry["restore_key"]["source"] == "env"
    assert "exactly one key" in dry["restore_key"]["problem"]
    assert any("cannot put that connection back" in w for w in dry["warnings"])

    result = run(rabbit.apply(model=MODEL))
    assert result["code"] == "restore_failed" and page.state is None
    assert "exactly one key" in result["restore"]["detail"]
    assert page.registered_keys == [BRIDGE_KEY]


@pytest.mark.parametrize("named", [False, True])
def test_a_whole_file_is_never_sent_as_the_key(
    keys: Path, base: Path, monkeypatch: pytest.MonkeyPatch, named: bool
) -> None:
    content = f"{RESTORE_KEY}\n# copied from the dashboard, ends in wxyz"
    path = write_key(base if named else keys, "openrouter.key", content)
    if named:
        monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(path))
    page = openrouter_page()
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)

    result = run(rabbit.apply(model=MODEL))

    assert result["code"] == "restore_failed" and page.state is None
    assert page.registered_keys == [BRIDGE_KEY]
    assert not any("\n" in key or "#" in key for key in page.registered_keys)
    assert RESTORE_KEY not in json.dumps(result)


def test_a_named_key_file_must_be_owner_only_and_not_a_symlink(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loose = write_key(base, "loose.key", RESTORE_KEY, mode=0o620)
    real = write_key(base / "real", "env.key", RESTORE_KEY)
    link = base / "link.key"
    link.symlink_to(real)

    for path, why in ((loose, "owner-only"), (link, "symlink")):
        monkeypatch.setenv("OS3_BRIDGE_RABBIT_RESTORE_KEY_FILE", str(path))
        page = openrouter_page()
        page.fail_register_times = 1
        rabbit, _, _ = engine(page)
        dry = run(rabbit.apply(model=MODEL, dry_run=True))
        assert why in dry["restore_key"]["problem"]
        result = run(rabbit.apply(model=MODEL))
        assert result["code"] == "restore_failed" and why in result["restore"]["detail"]
        assert page.registered_keys == [BRIDGE_KEY]


@pytest.mark.parametrize(
    ("masked", "key"),
    [
        ("****b", "sk-or-v1-FIXTURE-key-ab"),
        ("****ab", "sk-or-v1-FIXTURE-key-ab"),
        ("****wxyz", RESTORE_KEY),
        ("sk****wxyz", RESTORE_KEY),
        ("sk-or-v1-****xyz", RESTORE_KEY),
    ],
)
def test_a_single_file_in_rabbit_keys_is_not_picked_for_a_short_mask(
    base: Path, masked: str, key: str
) -> None:
    write_key(base / "rabbit-keys", "only.key", key)

    found = find_restore_key(masked)

    assert found.source == "none" and found.path is None and found.mask_too_short
    assert any("too little of the key" in w for w in found.warnings())

    page = page_with_mask(masked)
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)
    dry = run(rabbit.apply(model=MODEL, dry_run=True))
    assert dry["restore_key"] == {"source": "none", "path": None, "mask_too_short": True}
    assert not_restorable(dry)
    assert any("too little of the key" in w for w in dry["warnings"])

    result = run(rabbit.apply(model=MODEL))
    assert result["code"] == "restore_failed" and page.state is None
    assert "too little" in result["restore"]["detail"]
    assert page.registered_keys == [BRIDGE_KEY]


def test_plan_counts_candidates_without_reading_any_file(
    keys: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_key(keys, "a.key", RESTORE_KEY)
    write_key(keys, "b.key", OTHER_KEY)
    write_key(keys, "loose.key", OTHER_KEY, mode=0o644)
    monkeypatch.setattr(
        rabbit_keys, "_read_one_key", lambda *a: pytest.fail("plan must not read key files")
    )
    rabbit, _, _ = engine()

    plan = run(rabbit.plan())

    info = plan["restore_key"]
    assert info["source"] == "auto" and info["skipped_unreadable"] == 1
    assert info["candidates"] == 2 and info["dirs"] == [
        str(keys.resolve()),
        str((keys.parent / "rabbit-keys").resolve()),
    ]
    assert RESTORE_KEY not in json.dumps(plan)


def test_cli_plan_lists_the_candidate_count(
    os_page: FakeOs,
    keys: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_key(keys, "a.key", RESTORE_KEY)
    assert run_cli("plan") == 0
    out = capsys.readouterr().out
    assert "restore key:" in out and "candidate file(s)" in out
    assert RESTORE_KEY not in out and os_page.calls == []


def test_cli_dry_run_says_what_was_auto_detected(
    os_page: FakeOs,
    keys: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_SETUP", "on")
    path = write_key(keys, "openrouter.key", RESTORE_KEY)

    assert run_cli("setup", "--model", MODEL, "--dry-run") == 0

    out = capsys.readouterr().out
    assert f"restore key: auto-detected {path.resolve()}" in out
    assert "NEXUS_RABBIT_RESTORE_KEY_FILE" not in out


def test_cli_ambiguous_names_the_files(
    os_page: FakeOs,
    keys: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OS3_BRIDGE_RABBIT_SETUP", "on")
    first = write_key(keys, "a.key", RESTORE_KEY)
    second = write_key(keys, "b.key", SAME_MASK_KEY)

    assert run_cli("setup", "--model", MODEL, "--dry-run") == 0

    out = capsys.readouterr().out
    assert "auto-detected" not in out
    assert str(first.resolve()) in out and str(second.resolve()) in out


def test_no_key_value_reaches_stdout_logs_responses_or_the_backup(
    os_page: FakeOs,
    keys: Path,
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    write_key(keys, "openrouter.key", RESTORE_KEY)
    write_key(keys, "twin.key", SAME_MASK_KEY, mode=0o644)
    write_key(keys, "other.key", OTHER_KEY)
    secrets = [RESTORE_KEY, SAME_MASK_KEY, OTHER_KEY, "FIXTURE-restore-key", "FIXTURE-twin-key"]
    outputs: list[str] = []

    page = openrouter_page()
    page.fail_register_times = 1
    rabbit, _, _ = engine(page)
    outputs.append(json.dumps(run(rabbit.plan())))
    outputs.append(json.dumps(run(rabbit.apply(model=MODEL, dry_run=True))))
    outputs.append(json.dumps(run(rabbit.apply(model=MODEL))))
    outputs.append(json.dumps(run(rabbit.status())))
    outputs.append(backup_text(base))
    assert page.registered_keys == [BRIDGE_KEY, RESTORE_KEY]

    page.state = fixture("byok_get_custom_openrouter.json")["credential"]
    outputs.append(json.dumps(run(rabbit.restore())))
    outputs.append(json.dumps(run(rabbit.apply(model=MODEL))))
    outputs.append(json.dumps(run(rabbit.restore())))
    outputs.append(backup_text(base))

    door = Harness(openrouter_page())
    outputs.append(door.client.get("/dashboard/rabbit").text)
    outputs.append(post(door, "/dashboard/rabbit/apply", {"model": MODEL, "dry_run": True}).text)
    outputs.append(post(door, "/dashboard/rabbit/apply", {"model": MODEL}).text)
    outputs.append(post(door, "/dashboard/rabbit/restore", {}).text)

    monkeypatch.setenv("OS3_BRIDGE_RABBIT_SETUP", "on")
    assert cli.main(["rabbit", "plan"]) == 0
    cli.main(["rabbit", "setup", "--model", MODEL, "--dry-run"])
    captured = capsys.readouterr()
    outputs += [captured.out, captured.err, caplog.text]
    outputs += [p.read_text() for p in base.glob("*.json")]

    assert any("auto-detected" in text for text in outputs)
    for text in outputs:
        for secret in secrets:
            assert secret not in text
