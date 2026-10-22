"""Fresh installs use Nexus names. A machine on the pre-Nexus names keeps working untouched."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from nexus import config, service, setup_flow
from nexus.service import (
    LAUNCHD_LABEL,
    LEGACY_LAUNCHD_LABEL,
    LEGACY_SYSTEMD_UNIT,
    SYSTEMD_UNIT,
    CommandResult,
    LaunchdPlatform,
    Listener,
    SystemdPlatform,
    execute_start,
    execute_stop,
    launchd_label,
    render_installed_unit,
    systemd_unit_name,
)

ROOT = Path(__file__).resolve().parent.parent
KEY_ONE = "sk-proj-0123456789abcdef-one"
KEY_TWO = "sk-proj-0123456789abcdef-two"
KEY_THREE = "sk-proj-0123456789abcdef-three"
MAC_NEW = "Library/Application Support/Nexus"
MAC_OLD_KEYS = "Library/Application Support/GrokBot/os3-setup"
MAC_OLD_PROFILE = "Library/Application Support/GrokBot/os3-chrome-debug"


class FakeInit:
    """launchctl and systemctl in one: remembers which names are loaded and what ran."""

    def __init__(self, loaded: set[str] | None = None, refuse: set[str] | None = None) -> None:
        self.loaded = {name: 100 + i for i, name in enumerate(sorted(loaded or ()))}
        self.refuse = refuse or set()
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> CommandResult:
        self.calls.append(argv)
        if argv[0] == "launchctl":
            verb, target = argv[1], argv[2]
            name = target.rsplit("/", 1)[-1] if verb != "bootstrap" else Path(argv[3]).stem
            if verb == "print":
                pid = self.loaded.get(name)
                return (
                    CommandResult(0, f"\tpid = {pid}\n", "") if pid else CommandResult(113, "", "")
                )
            if verb == "bootstrap":
                if name in self.refuse:
                    return CommandResult(5, "", "bootstrap failed")
                self.loaded[name] = 200 + len(self.loaded)
            if verb == "bootout":
                self.loaded.pop(name, None)
            return CommandResult(0, "", "")
        verb = argv[2] if argv[1] == "--user" else argv[1]
        if verb == "show":
            pid = self.loaded.get(argv[3])
            if pid is None:
                return CommandResult(0, "LoadState=not-found\nActiveState=inactive\nMainPID=0", "")
            return CommandResult(0, f"LoadState=loaded\nActiveState=active\nMainPID={pid}", "")
        if verb == "enable":
            if argv[-1] in self.refuse:
                return CommandResult(1, "", "enable failed")
            self.loaded[argv[-1]] = 200 + len(self.loaded)
        if verb == "disable":
            self.loaded.pop(argv[-1], None)
        return CommandResult(0, "", "")

    def listeners(self) -> list[Listener]:
        return [Listener(pid, "python") for pid in self.loaded.values()]

    def mentioned(self, name: str) -> bool:
        return any(name in " ".join(argv) for argv in self.calls)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture(autouse=True)
def default_locations(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite points every key at a tmp file; these tests want the platform defaults."""
    for provider in ("OPENROUTER", "OPENAI", "ANTHROPIC", "XAI", "NOUS"):
        monkeypatch.delenv(f"OS3_BRIDGE_{provider}_KEY_FILE")
    config._warned_both.clear()
    service._warned_both.clear()


@pytest.fixture
def init(monkeypatch: pytest.MonkeyPatch) -> FakeInit:
    fake = FakeInit()
    monkeypatch.setattr(service, "_subprocess_run", fake)
    monkeypatch.setattr(service, "live_listeners", fake.listeners)
    return fake


def touch(path: Path, text: str = "secret-line\n", mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def agent(home: Path, label: str) -> Path:
    return touch(home / "Library" / "LaunchAgents" / f"{label}.plist", "<plist/>\n", 0o644)


def unit(home: Path, name: str) -> Path:
    return touch(home / ".config" / "systemd" / "user" / name, "[Unit]\n", 0o644)


def unit_text(home: Path, kind: str = "launchd") -> str:
    return render_installed_unit(
        kind,
        repo=ROOT,
        python=ROOT / ".venv" / "bin" / "python",
        env=service.door_environment("openrouter"),
        home=home,
    )


# --- key folders and the Chrome profile -------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "new_dir", "old_keys", "old_profile", "key_name"),
    [
        ("darwin", MAC_NEW, MAC_OLD_KEYS, MAC_OLD_PROFILE, ".openrouter-os3.key"),
        (
            "linux",
            ".config/nexus",
            ".config/os3-bridge",
            ".config/os3-bridge/os3-chrome-debug",
            "openrouter.key",
        ),
    ],
)
class TestFolders:
    def test_fresh_install_uses_the_nexus_folder(
        self, monkeypatch, home, platform, new_dir, old_keys, old_profile, key_name
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        assert config.openrouter_key_path() == home / new_dir / key_name
        assert config.rabbit_profile_dir() == home / new_dir / "os3-chrome-debug"

    def test_legacy_only_keeps_using_the_legacy_files(
        self, monkeypatch, home, platform, new_dir, old_keys, old_profile, key_name
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        touch(home / old_keys / key_name)
        (home / old_profile).mkdir(parents=True, exist_ok=True)
        assert config.openrouter_key_path() == home / old_keys / key_name
        assert config.rabbit_profile_dir() == home / old_profile
        assert config.openai_key_path().parent == home / old_keys

    def test_both_exist_new_wins_and_the_warning_is_logged_once(
        self, monkeypatch, home, platform, new_dir, old_keys, old_profile, key_name, caplog
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        touch(home / old_keys / key_name, "old\n")
        touch(home / new_dir / key_name, "new\n")
        with caplog.at_level(logging.WARNING, logger="nexus.config"):
            assert config.openrouter_key_path() == home / new_dir / key_name
            assert config.openrouter_key_path() == home / new_dir / key_name
        warnings = [r for r in caplog.records if "legacy" in r.getMessage()]
        assert len(warnings) == 1
        assert str(home / old_keys / key_name) in warnings[0].getMessage()
        assert (home / old_keys / key_name).read_text() == "old\n"

    def test_an_explicit_override_beats_both(
        self, monkeypatch, home, tmp_path, platform, new_dir, old_keys, old_profile, key_name
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        touch(home / old_keys / key_name)
        elsewhere = tmp_path / "mine.key"
        monkeypatch.setenv("NEXUS_OPENROUTER_KEY_FILE", str(elsewhere))
        monkeypatch.setenv("NEXUS_RABBIT_PROFILE", str(tmp_path / "profile"))
        assert config.openrouter_key_path() == elsewhere
        assert config.rabbit_profile_dir() == tmp_path / "profile"


def test_xdg_config_home_is_honoured_for_both_names(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.openai_key_path() == tmp_path / "xdg" / "nexus" / "openai.key"
    touch(tmp_path / "xdg" / "os3-bridge" / "openai.key")
    assert config.openai_key_path() == tmp_path / "xdg" / "os3-bridge" / "openai.key"


@pytest.mark.parametrize(
    ("platform", "new_dir", "old_keys", "old_profile", "nous", "openai"),
    [
        (
            "darwin",
            MAC_NEW,
            MAC_OLD_KEYS,
            MAC_OLD_PROFILE,
            ".nous-os3.key",
            ".openai-os3.key",
        ),
        (
            "linux",
            ".config/nexus",
            ".config/os3-bridge",
            ".config/os3-bridge/os3-chrome-debug",
            "nous.key",
            "openai.key",
        ),
    ],
)
class TestOneKeyFolder:
    def test_legacy_folder_only_keeps_every_new_key_and_the_profile_there(
        self, monkeypatch, home, platform, new_dir, old_keys, old_profile, nous, openai
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        touch(home / old_keys / nous, "nous-token\n")
        setup_flow.run_setup(backend="openai", api_key=KEY_ONE, non_interactive=True)
        assert (home / old_keys / openai).read_text() == KEY_ONE + "\n"
        assert not (home / new_dir).exists()
        assert config.openai_key_path() == home / old_keys / openai
        assert config.rabbit_profile_dir() == home / old_profile

    def test_new_folder_with_a_legacy_file_reads_legacy_and_writes_new(
        self, monkeypatch, home, platform, new_dir, old_keys, old_profile, nous, openai
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        touch(home / old_keys / openai, "sk-old-legacy\n")
        (home / new_dir).mkdir(parents=True)
        assert config.openai_key_path() == home / old_keys / openai
        setup_flow.run_setup(backend="openai", api_key=KEY_TWO, non_interactive=True)
        assert (home / new_dir / openai).read_text() == KEY_TWO + "\n"
        assert (home / old_keys / openai).read_text() == "sk-old-legacy\n"
        assert config.openai_key_path() == home / new_dir / openai

    @pytest.mark.parametrize("env_name", ["NEXUS_OPENAI_KEY_FILE", "OS3_BRIDGE_OPENAI_KEY_FILE"])
    @pytest.mark.parametrize("save", ["setup", "dashboard", "dashboard_key_only"])
    def test_an_override_into_the_legacy_folder_is_written_as_given(
        self,
        monkeypatch,
        home,
        platform,
        new_dir,
        old_keys,
        old_profile,
        nous,
        openai,
        env_name,
        save,
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        override = touch(home / old_keys / openai, "sk-old-legacy\n")
        (home / new_dir).mkdir(parents=True)
        monkeypatch.setenv(env_name, str(override))
        read_before = config.openai_key_path()
        if save == "setup":
            setup_flow.run_setup(backend="openai", api_key=KEY_ONE, non_interactive=True)
        else:
            setup_flow.apply_dashboard_choice(
                backend="openai", api_key=KEY_ONE, model=None, activate=save == "dashboard"
            )
        assert read_before == override == config.openai_key_path()
        assert override.read_text() == KEY_ONE + "\n"
        assert not (home / new_dir / openai).exists()

    def test_neither_folder_writes_the_new_one(
        self, monkeypatch, home, platform, new_dir, old_keys, old_profile, nous, openai
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        setup_flow.run_setup(backend="openai", api_key=KEY_THREE, non_interactive=True)
        assert (home / new_dir / openai).read_text() == KEY_THREE + "\n"
        assert not (home / old_keys).exists()
        assert config.rabbit_profile_dir() == home / new_dir / "os3-chrome-debug"


# --- launchd and systemd names ----------------------------------------------------------


def test_fresh_machine_gets_the_nexus_names(home, init) -> None:
    assert LAUNCHD_LABEL == "ai.nexus.server"
    assert SYSTEMD_UNIT == "nexus.service"
    mac = LaunchdPlatform(home)
    assert mac.label == "ai.nexus.server"
    assert mac.plist == home / "Library/LaunchAgents/ai.nexus.server.plist"
    linux = SystemdPlatform(home)
    assert linux.label == "nexus.service"
    assert linux.unit == home / ".config/systemd/user/nexus.service"
    rendered = unit_text(home)
    assert "<string>ai.nexus.server</string>" in rendered
    assert "grokbot" not in rendered.lower()
    assert execute_start(mac, rendered).code == 0
    assert mac.plist.is_file()
    assert init.mentioned("ai.nexus.server") and not init.mentioned("grokbot")
    assert not service.legacy_pinned()


def test_legacy_only_launchd_machine_keeps_its_label_and_is_left_running(home, init) -> None:
    plist = agent(home, LEGACY_LAUNCHD_LABEL)
    init.loaded[LEGACY_LAUNCHD_LABEL] = 77
    before = plist.read_text()
    platform = LaunchdPlatform(home, sleep=lambda _s: None)
    assert platform.label == LEGACY_LAUNCHD_LABEL == launchd_label(home)
    outcome = execute_start(platform, unit_text(home))
    assert outcome.code == 0 and "already listening" in outcome.message
    assert [argv[1] for argv in init.calls if argv[0] == "launchctl"] == ["print"]
    assert plist.read_text() == before
    assert not (home / "Library/LaunchAgents/ai.nexus.server.plist").exists()
    assert "<string>com.grokbot.os3-bridge</string>" in unit_text(home)


def test_stop_then_start_on_a_legacy_machine_never_changes_the_label(home, init) -> None:
    """The menu bar's Restart door runs stop then start, each in its own process."""
    plist = agent(home, LEGACY_LAUNCHD_LABEL)
    init.loaded[LEGACY_LAUNCHD_LABEL] = 77
    stop = execute_stop(LaunchdPlatform(home, sleep=lambda _s: None))
    assert stop.code == 0
    assert not plist.exists() and not init.loaded
    assert any(
        argv[:2] == ["launchctl", "bootout"] and argv[2].endswith(f"/{LEGACY_LAUNCHD_LABEL}")
        for argv in init.calls
    )
    again = LaunchdPlatform(home, sleep=lambda _s: None)
    assert again.label == LEGACY_LAUNCHD_LABEL
    assert execute_start(again, unit_text(home)).code == 0
    assert plist.is_file()
    assert "<string>com.grokbot.os3-bridge</string>" in plist.read_text()
    assert set(init.loaded) == {LEGACY_LAUNCHD_LABEL}
    assert not (home / "Library/LaunchAgents/ai.nexus.server.plist").exists()
    assert not init.mentioned("ai.nexus.server")


@pytest.mark.parametrize(
    ("make_platform", "legacy_name", "install"),
    [
        (LaunchdPlatform, LEGACY_LAUNCHD_LABEL, lambda h: agent(h, LEGACY_LAUNCHD_LABEL)),
        (SystemdPlatform, LEGACY_SYSTEMD_UNIT, lambda h: unit(h, LEGACY_SYSTEMD_UNIT)),
    ],
)
def test_the_pin_survives_stop_and_remove_for_the_next_process(
    home, init, make_platform, legacy_name, install
) -> None:
    path = install(home)
    make_platform(home).stop_and_remove()
    assert not path.exists()
    assert service.legacy_pinned()
    assert make_platform(home).label == legacy_name


def test_legacy_only_systemd_machine_keeps_its_unit(home, init) -> None:
    path = unit(home, LEGACY_SYSTEMD_UNIT)
    init.loaded[LEGACY_SYSTEMD_UNIT] = 77
    platform = SystemdPlatform(home)
    assert platform.label == systemd_unit_name(home) == LEGACY_SYSTEMD_UNIT
    assert execute_start(platform, unit_text(home, "systemd")).code == 0
    assert path.is_file() and not (home / ".config/systemd/user/nexus.service").exists()
    assert execute_stop(platform).code == 0
    assert not path.exists()
    assert SystemdPlatform(home).label == LEGACY_SYSTEMD_UNIT
    assert not init.mentioned(" nexus.service")


def test_both_installed_new_wins_with_a_warning_and_the_legacy_one_is_untouched(
    home, init, caplog
) -> None:
    old = agent(home, LEGACY_LAUNCHD_LABEL)
    new = agent(home, LAUNCHD_LABEL)
    init.loaded.update({LEGACY_LAUNCHD_LABEL: 77, LAUNCHD_LABEL: 78})
    with caplog.at_level(logging.WARNING, logger="nexus.service"):
        platform = LaunchdPlatform(home, sleep=lambda _s: None)
        assert platform.label == LAUNCHD_LABEL
        platform.stop_and_remove()
    assert any(LEGACY_LAUNCHD_LABEL in r.getMessage() for r in caplog.records)
    assert not new.exists() and old.exists()
    assert LEGACY_LAUNCHD_LABEL in init.loaded and LAUNCHD_LABEL not in init.loaded
    assert not init.mentioned(LEGACY_LAUNCHD_LABEL)


@pytest.mark.parametrize("installed", [LAUNCHD_LABEL, LEGACY_LAUNCHD_LABEL])
def test_uninstall_removes_whichever_launchd_agent_exists(home, init, installed) -> None:
    path = agent(home, installed)
    init.loaded[installed] = 77
    outcome = execute_stop(LaunchdPlatform(home, sleep=lambda _s: None))
    assert outcome.code == 0 and "Stopped packaged nexus" in outcome.message
    assert not path.exists() and not init.loaded


@pytest.mark.parametrize("installed", [SYSTEMD_UNIT, LEGACY_SYSTEMD_UNIT])
def test_uninstall_removes_whichever_systemd_unit_exists(home, init, installed) -> None:
    path = unit(home, installed)
    init.loaded[installed] = 77
    assert execute_stop(SystemdPlatform(home)).code == 0
    assert not path.exists() and not init.loaded


def test_service_label_override_still_wins_over_a_legacy_agent(home, init, monkeypatch) -> None:
    agent(home, LEGACY_LAUNCHD_LABEL)
    monkeypatch.setenv("NEXUS_SERVICE_LABEL", "com.example.nexus-test")
    platform = LaunchdPlatform(home)
    assert platform.label == launchd_label(home) == "com.example.nexus-test"
    assert "<string>com.example.nexus-test</string>" in unit_text(home)
    platform.stop_and_remove()
    assert not service.legacy_pinned()
    assert not init.mentioned(LEGACY_LAUNCHD_LABEL)


@pytest.mark.parametrize(
    ("make_platform", "legacy_name", "install", "new_name"),
    [
        (
            LaunchdPlatform,
            LEGACY_LAUNCHD_LABEL,
            lambda h: agent(h, LEGACY_LAUNCHD_LABEL),
            LAUNCHD_LABEL,
        ),
        (
            SystemdPlatform,
            LEGACY_SYSTEMD_UNIT,
            lambda h: unit(h, LEGACY_SYSTEMD_UNIT),
            SYSTEMD_UNIT,
        ),
    ],
)
def test_a_failed_pin_write_is_reported_to_the_user_by_stop(
    monkeypatch, tmp_path, home, init, make_platform, legacy_name, install, new_name
) -> None:
    install(home)
    init.loaded[legacy_name] = 77
    monkeypatch.setattr(service, "legacy_pin_path", lambda: tmp_path / "no-such-dir" / "pin")
    outcome = execute_stop(make_platform(home))
    assert outcome.code == 0
    assert "Warning: could not write" in outcome.message
    assert new_name in outcome.message and legacy_name in outcome.message
    assert not service.legacy_pinned()


def test_a_recorded_pin_adds_no_warning(home, init) -> None:
    agent(home, LEGACY_LAUNCHD_LABEL)
    init.loaded[LEGACY_LAUNCHD_LABEL] = 77
    outcome = execute_stop(LaunchdPlatform(home, sleep=lambda _s: None))
    assert "Warning" not in outcome.message


@pytest.mark.parametrize("backend", sorted(setup_flow.API_BACKENDS))
def test_each_providers_write_override_is_the_variable_its_read_path_honours(
    monkeypatch, tmp_path, backend
) -> None:
    spec = setup_flow.API_BACKENDS[backend]
    elsewhere = tmp_path / "mine.key"
    name = f"NEXUS_{spec.env_var.removesuffix('_API_KEY')}_KEY_FILE"
    monkeypatch.setenv(name, str(elsewhere))
    assert spec.key_path() == elsewhere == spec.key_write_path()
