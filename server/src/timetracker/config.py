"""Strict configuration loading and location resolution."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

log = logging.getLogger(__name__)

DEFAULT_CONFIG = """\
# The server is unauthenticated; bind to a trusted interface.
bind_address = "127.0.0.1"
port = 8000
# data_dir defaults to the systemd state directory or XDG data directory.
# data_dir = "/path/to/timetracker"
verify_journal_on_startup = true
verify_cross_check_on_startup = true
deep_check = "startup"
# At least one threshold is required when deep_check = "interval".
# deep_check_days = 7
# deep_check_events = 1000
"""

_KEYS = {
    "bind_address", "port", "data_dir", "verify_journal_on_startup",
    "verify_cross_check_on_startup", "deep_check", "deep_check_days",
    "deep_check_events",
}


class ConfigurationError(ValueError):
    """An invalid setting or configuration file."""


@dataclass(frozen=True)
class Settings:
    bind_address: str
    port: int
    data_dir: Path
    verify_journal_on_startup: bool
    verify_cross_check_on_startup: bool
    deep_check: str
    deep_check_days: int | None
    deep_check_events: int | None


def _systemd_dir(value: str, name: str) -> Path:
    # systemd separates multiple directory paths with a colon.
    first = value.split(":")[0]
    if not first or not Path(first).is_absolute():
        raise ConfigurationError(f"{name} must contain an absolute directory path")
    return Path(first)


def config_path(env: Mapping[str, str], override: str | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser()
    if "TIMETRACKER_CONFIG" in env:
        return Path(env["TIMETRACKER_CONFIG"]).expanduser()
    if "CONFIGURATION_DIRECTORY" in env:
        return _systemd_dir(env["CONFIGURATION_DIRECTORY"], "CONFIGURATION_DIRECTORY") / "config.toml"
    return Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser() / "timetracker" / "config.toml"


def _default_data_dir(env: Mapping[str, str]) -> Path:
    if "STATE_DIRECTORY" in env:
        return _systemd_dir(env["STATE_DIRECTORY"], "STATE_DIRECTORY")
    return Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share").expanduser() / "timetracker"


def init_config(path: Path) -> None:
    """Create defaults exclusively; never replace an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(DEFAULT_CONFIG)


def _env_value(key: str, value: str) -> object:
    if key in {"verify_journal_on_startup", "verify_cross_check_on_startup"}:
        if value.lower() not in {"true", "false"}:
            raise ConfigurationError(f"{key}: expected true or false")
        return value.lower() == "true"
    if key in {"port", "deep_check_days", "deep_check_events"}:
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigurationError(f"{key}: expected an integer") from exc
    return value


def load_config(
    *, env: Mapping[str, str] | None = None, config: str | None = None,
    overrides: Mapping[str, object] | None = None,
) -> Settings:
    env = os.environ if env is None else env
    path = config_path(env, config)
    try:
        with path.open("rb") as handle:
            values = tomllib.load(handle)
    except FileNotFoundError:
        if "CONFIGURATION_DIRECTORY" in env:
            log.warning("Configuration file %s not found; using defaults", path)
        else:
            try:
                init_config(path)
            except FileExistsError:
                return load_config(env=env, config=str(path), overrides=overrides)
        values = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"configuration {path}: {exc}") from exc
    unknown = values.keys() - _KEYS
    if unknown:
        raise ConfigurationError(f"unknown configuration key: {sorted(unknown)[0]}")

    merged: dict[str, object] = {
        "bind_address": "127.0.0.1", "port": 8000,
        "data_dir": _default_data_dir(env),
        "verify_journal_on_startup": True, "verify_cross_check_on_startup": True,
        "deep_check": "startup", "deep_check_days": None, "deep_check_events": None,
    }
    merged.update(values)
    for key in _KEYS:
        name = f"TIMETRACKER_{key.upper()}"
        if name in env:
            merged[key] = _env_value(key, env[name])
    if overrides:
        unknown = overrides.keys() - _KEYS
        if unknown:
            raise ConfigurationError(f"unknown configuration key: {sorted(unknown)[0]}")
        merged.update(overrides)

    address = merged["bind_address"]
    if not isinstance(address, str) or not address.strip():
        raise ConfigurationError("bind_address: expected a nonempty string")
    port = merged["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigurationError("port: expected an integer from 1 to 65535")
    data_dir = merged["data_dir"]
    if not isinstance(data_dir, (str, Path)) or not str(data_dir).strip():
        raise ConfigurationError("data_dir: expected a nonempty path")
    flags = []
    for key in ("verify_journal_on_startup", "verify_cross_check_on_startup"):
        value = merged[key]
        if type(value) is not bool:
            raise ConfigurationError(f"{key}: expected a boolean")
        flags.append(value)
    mode = merged["deep_check"]
    if not isinstance(mode, str) or mode not in {"startup", "interval", "manual"}:
        raise ConfigurationError("deep_check: expected startup, interval, or manual")
    thresholds: list[int | None] = []
    for key in ("deep_check_days", "deep_check_events"):
        value = merged[key]
        if value is not None and (type(value) is not int or value <= 0):
            raise ConfigurationError(f"{key}: expected a positive integer")
        thresholds.append(value)
    if mode == "interval" and all(value is None for value in thresholds):
        raise ConfigurationError("deep_check: interval requires deep_check_days or deep_check_events")
    return Settings(address, port, Path(data_dir).expanduser(), flags[0], flags[1],
                    mode, thresholds[0], thresholds[1])
