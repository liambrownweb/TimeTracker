from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from timetracker.cli import main
from timetracker.config import ConfigurationError, config_path, init_config, load_config
from timetracker.domain.entries import EntryCreate, EntryUpdate
from timetracker.service.recovery import FindingKind
from timetracker.storage import JsonlEntryStore, StorageCorruptionError
from timetracker.verification import (
    CheckState, VerificationError, deep_check_due, open_server, read_check_state,
    verify, write_check_state,
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data")}


def test_default_config_and_xdg_locations(env: dict[str, str]) -> None:
    settings = load_config(env=env)
    path = config_path(env)
    assert path.is_file()
    assert settings.data_dir == Path(env["XDG_DATA_HOME"]) / "timetracker"
    assert (settings.bind_address, settings.port, settings.deep_check) == ("127.0.0.1", 8000, "startup")
    assert settings.verify_journal_on_startup and settings.verify_cross_check_on_startup
    before = path.read_bytes()
    load_config(env=env)
    assert path.read_bytes() == before
    with pytest.raises(FileExistsError):
        init_config(path)


def test_systemd_locations_and_missing_config_is_not_created(env: dict[str, str], tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    env["CONFIGURATION_DIRECTORY"] = f"{tmp_path}/etc:{tmp_path}/other"
    env["STATE_DIRECTORY"] = str(tmp_path / "state")
    settings = load_config(env=env)
    assert config_path(env) == tmp_path / "etc" / "config.toml"
    assert not config_path(env).exists()
    assert settings.data_dir == tmp_path / "state"
    assert "not found" in caplog.text
    init_config(config_path(env))
    assert config_path(env).exists()


def test_systemd_user_data_uses_xdg_path(env: dict[str, str], tmp_path: Path) -> None:
    env["CONFIGURATION_DIRECTORY"] = str(tmp_path / "user-config")
    assert load_config(env=env).data_dir == Path(env["XDG_DATA_HOME"]) / "timetracker"


def test_precedence(env: dict[str, str], tmp_path: Path) -> None:
    config = tmp_path / "custom.toml"
    config.write_text('port = 1234\ndata_dir = "/file"\n', encoding="utf-8")
    env.update(TIMETRACKER_CONFIG=str(config), TIMETRACKER_PORT="2345", TIMETRACKER_DATA_DIR="/env")
    settings = load_config(env=env, overrides={"port": 3456})
    assert settings.port == 3456
    assert settings.data_dir == Path("/env")
    assert config_path(env, "/explicit") == Path("/explicit")


@pytest.mark.parametrize("text,key", [
    ("wat = 1", "wat"), ("port = true", "port"),
    ('bind_address = ""', "bind_address"), ("verify_journal_on_startup = 1", "verify_journal_on_startup"),
    ('deep_check = "weekly"', "deep_check"), ("deep_check_days = 0", "deep_check_days"),
    ('deep_check = "interval"', "deep_check"), ("data_dir = 1", "data_dir"),
])
def test_invalid_config_names_key(env: dict[str, str], tmp_path: Path, text: str, key: str) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=key):
        load_config(env=env, config=str(path))


def test_invalid_environment_reports_key(env: dict[str, str]) -> None:
    env["TIMETRACKER_VERIFY_JOURNAL_ON_STARTUP"] = "maybe"
    with pytest.raises(ConfigurationError, match="verify_journal_on_startup"):
        load_config(env=env)


def _settings(env: dict[str, str], **options: object):
    return load_config(env=env, overrides=options)


def _create(coordinator):
    entry = EntryCreate(id=uuid4(), task="work", duration_seconds=10)
    coordinator.create(entry)
    return entry


def test_interval_days_events_and_persistence(env: dict[str, str]) -> None:
    settings = _settings(env, deep_check="interval", deep_check_days=7, deep_check_events=2)
    settings.data_dir.mkdir(parents=True)
    now = datetime(2026, 11, 12, tzinfo=timezone.utc)
    assert deep_check_due(settings, 0, now)
    write_check_state(settings.data_dir, CheckState(now, 10))
    assert read_check_state(settings.data_dir) == CheckState(now, 10)
    assert not deep_check_due(settings, 11, now + timedelta(days=6))
    assert deep_check_due(settings, 12, now + timedelta(days=6))
    assert deep_check_due(settings, 11, now + timedelta(days=7))
    with pytest.raises(VerificationError, match="ahead"):
        deep_check_due(settings, 9, now)


def test_modes_and_initial_deep_check(env: dict[str, str]) -> None:
    settings = _settings(env, deep_check="interval", deep_check_events=2)
    with open_server(settings) as server:
        entry = _create(server)
    state = read_check_state(settings.data_dir)
    assert state is not None and state.sequence == 0
    with open_server(settings):
        pass
    assert read_check_state(settings.data_dir) == state
    with open_server(settings) as server:
        server.update(entry.id, EntryUpdate(task="next"))
    with open_server(settings):
        pass
    assert read_check_state(settings.data_dir).sequence == 2
    manual = _settings(env, deep_check="manual")
    assert not deep_check_due(manual, 2, datetime.now(timezone.utc))
    assert deep_check_due(_settings(env), 2, datetime.now(timezone.utc))


def test_failed_deep_check_does_not_update_state(env: dict[str, str]) -> None:
    settings = _settings(env)
    with open_server(settings) as server:
        entry = _create(server)
    previous = read_check_state(settings.data_dir)
    store = JsonlEntryStore(settings.data_dir)
    record = store.get(entry.id)
    assert record is not None
    store.restore([record.model_copy(update={"task": "tampered"})])
    assert verify(settings, deep=True)[0].kind is FindingKind.FIELD_MISMATCH
    assert read_check_state(settings.data_dir) == previous
    with pytest.raises(VerificationError, match="deep verification failed"):
        with open_server(settings):
            pass
    assert read_check_state(settings.data_dir) == previous
    assert JsonlEntryStore(settings.data_dir).get(entry.id).task == "tampered"


def test_verify_read_only_and_torn_tail(env: dict[str, str]) -> None:
    settings = _settings(env, deep_check="manual")
    with open_server(settings) as server:
        _create(server)
    journal_file = next((settings.data_dir / "journal").rglob("*.jsonl"))
    journal_file.write_bytes(journal_file.read_bytes() + b'{"unfinished":')
    before = journal_file.read_bytes()
    with pytest.raises(StorageCorruptionError, match="missing its newline"):
        verify(settings, deep=True)
    assert journal_file.read_bytes() == before
    assert not (settings.data_dir / "quarantine").exists()
    assert read_check_state(settings.data_dir) is None
    with open_server(settings):
        pass
    assert journal_file.read_bytes() != before
    assert (settings.data_dir / "quarantine").exists()


def test_disabled_journal_verification_and_manual_verify(env: dict[str, str]) -> None:
    settings = _settings(env, deep_check="manual", verify_journal_on_startup=False)
    with open_server(settings) as server:
        _create(server)
    path = next((settings.data_dir / "journal").rglob("*.jsonl"))
    event = json.loads(path.read_text())
    event["checksum"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(event) + "\n")
    with open_server(settings):
        pass
    with pytest.raises(StorageCorruptionError, match="checksum"):
        verify(settings)


def test_manual_deep_verify_updates_state_and_data_lock_excludes_verification(env: dict[str, str]) -> None:
    settings = _settings(env, deep_check="manual")
    with open_server(settings) as server:
        _create(server)
        with pytest.raises(VerificationError, match="already in use"):
            verify(settings, deep=True)
    assert verify(settings, deep=True) == []
    assert read_check_state(settings.data_dir).sequence == 1


def test_cli_exit_codes_and_config_override(env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in ("CONFIGURATION_DIRECTORY", "STATE_DIRECTORY", "TIMETRACKER_CONFIG", "TIMETRACKER_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    assert main(["init-config"]) == 0
    assert main(["init-config"]) == 1
    settings = load_config(env=env)
    with open_server(settings) as server:
        _create(server)
    assert main(["verify", "--deep"]) == 0
    assert "Verification passed (deep)" in capsys.readouterr().out


def test_invalid_state_does_not_silently_run_or_overwrite(env: dict[str, str]) -> None:
    settings = _settings(env, deep_check="interval", deep_check_days=1)
    settings.data_dir.mkdir(parents=True)
    path = settings.data_dir / "verification-state.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(VerificationError, match="invalid verification state"):
        with open_server(settings):
            pass
    assert path.read_text() == "{"


def test_interrupted_state_replacement_preserves_previous_state(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(env, deep_check="manual")
    settings.data_dir.mkdir(parents=True)
    original = CheckState(datetime(2026, 11, 1, tzinfo=timezone.utc), 0)
    write_check_state(settings.data_dir, original)

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("replacement interrupted")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted"):
        write_check_state(settings.data_dir, CheckState(datetime.now(timezone.utc), 1))
    assert read_check_state(settings.data_dir) == original
    assert not list(settings.data_dir.glob(".verification-state.*.tmp"))


def test_cli_mismatch_is_nonzero_and_never_repairs(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in ("CONFIGURATION_DIRECTORY", "STATE_DIRECTORY", "TIMETRACKER_CONFIG", "TIMETRACKER_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    settings = load_config(env=env)
    with open_server(settings) as server:
        entry = _create(server)
    store = JsonlEntryStore(settings.data_dir)
    record = store.get(entry.id)
    assert record is not None
    store.restore([record.model_copy(update={"task": "wrong"})])
    path = next((settings.data_dir / "entries").rglob("*.jsonl"))
    before = path.read_bytes()
    assert main(["verify", "--deep"]) == 1
    assert "field_mismatch" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_deep_check_forces_full_journal_verification(env: dict[str, str]) -> None:
    manual = _settings(env, deep_check="manual")
    with open_server(manual) as server:
        _create(server)
    path = next((manual.data_dir / "journal").rglob("*.jsonl"))
    event = json.loads(path.read_text())
    event["checksum"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(event) + "\n")
    settings = _settings(env, verify_journal_on_startup=False)
    with pytest.raises(StorageCorruptionError, match="checksum"):
        with open_server(settings):
            pass


def test_disabled_cross_check_is_still_available_on_demand(env: dict[str, str]) -> None:
    settings = _settings(env, deep_check="manual", verify_cross_check_on_startup=False)
    with open_server(settings) as server:
        first = _create(server)
        _create(server)
    store = JsonlEntryStore(settings.data_dir)
    record = store.get(first.id)
    assert record is not None
    store.restore([record.model_copy(update={"revision": 99})])

    with open_server(settings) as server:
        assert not server.recovery_report.ran_cross_check
    findings = verify(settings)
    assert findings[0].kind is FindingKind.METADATA_MISMATCH
    assert JsonlEntryStore(settings.data_dir).get(first.id).revision == 99


def test_verify_reports_corrupt_records_without_quarantining(env: dict[str, str]) -> None:
    settings = _settings(env, deep_check="manual")
    with open_server(settings) as server:
        _create(server)
    path = next((settings.data_dir / "entries").rglob("*.jsonl"))
    path.write_text("invalid\n")
    with pytest.raises(StorageCorruptionError):
        verify(settings)
    assert path.read_text() == "invalid\n"
    assert not (settings.data_dir / "quarantine").exists()


def test_manual_verify_rejects_bad_state_without_overwriting(env: dict[str, str]) -> None:
    settings = _settings(env, deep_check="manual")
    with open_server(settings):
        pass
    path = settings.data_dir / "verification-state.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(VerificationError, match="invalid verification state"):
        verify(settings, deep=True)
    assert path.read_text() == "{"
