"""Configuration loading: file resolution, validation and legacy handling."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tapo_config import ConfigError, load_config  # noqa: E402

CREDENTIALS = {"email": "user@example.com", "password": "secret"}
DEVICE = {"name": "Washer", "device_type": "P115", "ip_addr": "192.168.1.11"}


def write(tmp_path: Path, payload, name="config.json") -> Path:
    path = tmp_path / name
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )
    return path


def test_minimal_config_loads(tmp_path):
    config = load_config(
        write(tmp_path, {"tapo_credentials": CREDENTIALS, "devices": [DEVICE]})
    )

    assert config.email == "user@example.com"
    assert len(config.devices) == 1
    assert config.api_keys == ()


def test_the_old_two_key_config_gets_a_pointed_error(tmp_path):
    path = write(
        tmp_path,
        {"tapo_api_url": "http://192.168.0.8:1180", "login_password": "potatoes"},
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "old configuration format" in message
    assert "config.sample.json" in message


def test_a_v043_tapo_rest_config_loads_unchanged(tmp_path):
    """The production devices.json shape: flat server_password, parsed and ignored."""
    config = load_config(
        write(
            tmp_path,
            {
                "tapo_credentials": CREDENTIALS,
                "server_password": "potatoes",
                "devices": [DEVICE],
            },
        )
    )

    assert config.devices[0].name == "Washer"
    assert config.api_keys == ()


def test_substract_is_preserved(tmp_path):
    devices = [DEVICE, {**DEVICE, "name": "TV", "substract": "Washer"}]
    config = load_config(write(tmp_path, {"tapo_credentials": CREDENTIALS, "devices": devices}))

    assert config.devices[1].substract == "Washer"


def test_short_api_key_is_rejected(tmp_path):
    payload = {
        "tapo_credentials": CREDENTIALS,
        "server": {"api_keys": [{"name": "short", "key": "abc"}]},
        "devices": [DEVICE],
    }

    with pytest.raises(ConfigError, match="too short"):
        load_config(write(tmp_path, payload))


def test_non_alphanumeric_api_key_is_rejected(tmp_path):
    payload = {
        "tapo_credentials": CREDENTIALS,
        "server": {"api_keys": [{"name": "bad", "key": "REPLACE_ME_" + "a" * 32}]},
        "devices": [DEVICE],
    }

    with pytest.raises(ConfigError, match="non-alphanumeric"):
        load_config(write(tmp_path, payload))


def test_valid_api_key_is_accepted(tmp_path):
    key = "0123456789abcdef0123456789abcdef"
    payload = {
        "tapo_credentials": CREDENTIALS,
        "server": {"api_keys": [{"name": "hass", "key": key}]},
        "devices": [DEVICE],
    }
    config = load_config(write(tmp_path, payload))

    assert config.is_valid_api_key(key)
    assert not config.is_valid_api_key("z" * 32)


def test_unknown_device_type_is_rejected(tmp_path):
    payload = {
        "tapo_credentials": CREDENTIALS,
        "devices": [{**DEVICE, "device_type": "P999"}],
    }

    with pytest.raises(ConfigError, match="Unknown device_type"):
        load_config(write(tmp_path, payload))


def test_device_type_is_case_insensitive(tmp_path):
    payload = {"tapo_credentials": CREDENTIALS, "devices": [{**DEVICE, "device_type": "p115"}]}
    config = load_config(write(tmp_path, payload))

    assert config.devices[0].device_type == "P115"


def test_duplicate_device_names_are_rejected(tmp_path):
    payload = {"tapo_credentials": CREDENTIALS, "devices": [DEVICE, DEVICE]}

    with pytest.raises(ConfigError, match="Duplicate device name"):
        load_config(write(tmp_path, payload))


def test_missing_credentials_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="credentials are missing"):
        load_config(write(tmp_path, {"devices": [DEVICE]}))


def test_environment_overrides_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("TAPO_EMAIL", "env@example.com")
    monkeypatch.setenv("TAPO_PASSWORD", "envsecret")
    config = load_config(write(tmp_path, {"tapo_credentials": CREDENTIALS, "devices": [DEVICE]}))

    assert config.email == "env@example.com"
    assert config.password == "envsecret"


def test_api_keys_can_come_from_the_environment(tmp_path, monkeypatch):
    key = "f" * 40
    monkeypatch.setenv("TAPO_API_KEYS", f" {key} ,")
    config = load_config(write(tmp_path, {"tapo_credentials": CREDENTIALS, "devices": [DEVICE]}))

    assert config.is_valid_api_key(key)


def test_a_leading_comment_line_is_tolerated(tmp_path):
    raw = "// my devices\n" + json.dumps({"tapo_credentials": CREDENTIALS, "devices": [DEVICE]})
    config = load_config(write(tmp_path, raw))

    assert len(config.devices) == 1


def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.json")
