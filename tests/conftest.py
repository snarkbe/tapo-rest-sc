"""Test fixtures: a service backed by fake devices, so nothing touches the LAN."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tapo_config import load_config  # noqa: E402
from tapo_devices import (  # noqa: E402
    ActionError,
    DeviceError,
    DeviceRegistry,
    normalise_payload,
)
from tapo_state import ServiceState  # noqa: E402

VALID_API_KEY = "a" * 32


class FakeLight:
    """Stands in for python-kasa's Light module."""

    def __init__(self):
        self.calls = []

    async def set_brightness(self, level):
        self.calls.append(("set_brightness", level))

    async def set_color_temp(self, kelvin):
        self.calls.append(("set_color_temp", kelvin))

    async def set_hsv(self, hue, saturation):
        self.calls.append(("set_hsv", hue, saturation))


class FakeLightEffect:
    """Stands in for python-kasa's LightEffect module."""

    def __init__(self, effect_list=()):
        self.effect_list = list(effect_list)
        self.effect = None

    async def set_effect(self, name):
        self.effect = name


class FakeDevice:
    """Stands in for a TapoDevice, answering from canned data."""

    def __init__(self, entry, power=None, error=None, payloads=None, modules=None):
        self.entry = entry
        self.power = power
        self.error = error
        self.payloads = payloads or {}
        # Which python-kasa modules this device pretends to carry. An absent
        # module is how a real device reports a feature it does not have.
        self.modules = modules or {}
        self.refreshed = 0
        self.calls = []

    @property
    def name(self):
        return self.entry.name

    @property
    def device_type(self):
        return self.entry.device_type

    async def raw(self, method, params=None):
        self.calls.append((method, params))
        if self.error:
            raise DeviceError(self.error)
        if method in self.payloads:
            # The real TapoDevice.raw normalises before returning; so must this.
            return normalise_payload(self.payloads[method])
        if method == "get_current_power":
            return {"current_power": self.power}
        if method == "set_device_info":
            return {}
        raise DeviceError(f"unexpected method {method}")

    async def module(self, module_name):
        if self.error:
            raise DeviceError(self.error)
        module = self.modules.get(str(module_name))
        if module is None:
            raise ActionError(
                f"Device '{self.name}' ({self.device_type}) does not support "
                f"the '{module_name}' feature"
            )
        return module

    async def refresh_session(self):
        if self.error:
            raise DeviceError(self.error)
        self.refreshed += 1

    async def disconnect(self):
        pass


def write_config(tmp_path: Path, devices, *, api_keys=(VALID_API_KEY,)) -> Path:
    payload = {
        "tapo_credentials": {"email": "user@example.com", "password": "secret"},
        "server": {
            "api_keys": [
                {"name": f"key{index}", "key": key}
                for index, key in enumerate(api_keys)
            ]
        },
        "devices": devices,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def build_service(config_path: Path, powers: dict) -> ServiceState:
    """A ServiceState whose registry holds FakeDevices instead of real ones."""
    config = load_config(config_path)
    service = ServiceState()
    service.config = config
    service.registry = DeviceRegistry(
        devices={
            entry.name: FakeDevice(entry, **powers.get(entry.name, {"power": 0}))
            for entry in config.devices
        }
    )
    service.init_error = None
    return service


@pytest.fixture
def make_client(tmp_path):
    """Build a TestClient over the real app with a faked-out service."""
    from fastapi.testclient import TestClient

    import taposc

    def _make(devices, powers=None, api_keys=(VALID_API_KEY,)):
        path = write_config(tmp_path, devices, api_keys=api_keys)
        service = build_service(path, powers or {})
        taposc.app.state.service = service
        # No `with` block: that would run the lifespan and hit the network.
        return TestClient(taposc.app), service

    return _make
