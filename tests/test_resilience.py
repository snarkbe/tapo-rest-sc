"""Session expiry must not surface to callers.

The deployment this replaces answered `Session timeout` for every device until
someone forced a reconnection by hand. A dropped session is instead
re-established transparently on the next request.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tapo_config import DeviceEntry  # noqa: E402
from tapo_devices import DeviceError, TapoDevice  # noqa: E402

ENTRY = DeviceEntry(name="Washer", device_type="P115", ip_addr="192.168.0.45")


class FakeProtocol:
    def __init__(self, script):
        self.script = list(script)
        self.queries = []

    async def query(self, request):
        self.queries.append(request)
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, protocol):
        self.protocol = protocol
        self.disconnected = 0

    async def disconnect(self):
        self.disconnected += 1


def _device(clients):
    """A TapoDevice whose connection attempts hand back the given clients."""
    device = TapoDevice(ENTRY, credentials=None)  # type: ignore[arg-type]
    remaining = list(clients)

    async def fake_connect():
        return remaining.pop(0)

    device._connect = fake_connect  # type: ignore[method-assign]
    return device


@pytest.mark.asyncio
async def test_expired_session_is_retried_on_a_fresh_connection():
    stale = FakeClient(FakeProtocol([Exception("Session timeout")]))
    fresh = FakeClient(FakeProtocol([{"get_current_power": {"current_power": 42}}]))
    device = _device([stale, fresh])

    assert await device.raw("get_current_power") == {"current_power": 42}
    assert stale.disconnected == 1
    assert len(fresh.protocol.queries) == 1


@pytest.mark.asyncio
async def test_a_persistent_failure_still_raises_after_one_retry():
    first = FakeClient(FakeProtocol([Exception("boom")]))
    second = FakeClient(FakeProtocol([Exception("boom again")]))
    device = _device([first, second])

    with pytest.raises(DeviceError) as excinfo:
        await device.raw("get_current_power")

    assert "boom again" in str(excinfo.value)
    assert "get_current_power failed on 'Washer'" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_healthy_call_does_not_reconnect():
    client = FakeClient(FakeProtocol([{"get_current_power": {"current_power": 7}}]))
    device = _device([client])

    assert await device.raw("get_current_power") == {"current_power": 7}
    assert client.disconnected == 0


@pytest.mark.asyncio
async def test_refresh_session_replaces_the_connection():
    old = FakeClient(FakeProtocol([{"get_current_power": {"current_power": 1}}]))
    new = FakeClient(FakeProtocol([{"get_current_power": {"current_power": 2}}]))
    device = _device([old, new])

    await device.raw("get_current_power")
    await device.refresh_session()

    assert old.disconnected == 1
    assert await device.raw("get_current_power") == {"current_power": 2}
