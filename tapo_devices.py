"""Talking to Tapo devices with python-kasa.

One object per configured device, holding a lazily-established and then cached
connection, plus the handful of operations the API exposes.

There is no table of which model supports which action: python-kasa already
knows. `TapoDevice.module()` raises `ActionError` when a device does not carry
the feature being asked for, which the API turns into a 400.

Payloads are the device's own JSON. One normalisation is applied on top:
`ssid`/`nickname` are base64-decoded and `local_time` is rendered in ISO form,
so responses are readable rather than faithful to the wire format.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from kasa import Credentials, Device, DeviceConfig, Discover, Module

from tapo_config import Config, DeviceEntry

logger = logging.getLogger(__name__)

# How long to wait for the initial protocol probe of a single device.
CONNECT_TIMEOUT_SECONDS = 30

# The `interval` values `get_energy_data` accepts, in minutes.
ENERGY_INTERVALS: dict[str, int] = {
    "hourly": 60,
    "daily": 1440,
    "monthly": 43200,
}


class DeviceError(Exception):
    """A device could not be reached, or refused a command."""


class ActionError(Exception):
    """The caller asked for something this device cannot do (a 400, not a 502)."""


# ---------------------------------------------------------------------------
# Payload normalisation
# ---------------------------------------------------------------------------

_BASE64_FIELDS = ("ssid", "nickname")


def _decode_base64_field(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return value


def _normalise_local_time(value: Any) -> Any:
    """Devices report `2026-08-09 18:29:18`; render it ISO."""
    if not isinstance(value, str):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").isoformat()
    except ValueError:
        return value


def normalise_payload(payload: Any) -> Any:
    """Decode base64 fields and ISO-format timestamps, recursively."""
    if isinstance(payload, list):
        return [normalise_payload(item) for item in payload]
    if not isinstance(payload, dict):
        return payload

    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _BASE64_FIELDS:
            result[key] = _decode_base64_field(value)
        elif key == "local_time":
            result[key] = _normalise_local_time(value)
        else:
            result[key] = normalise_payload(value)
    return result


def _local_timestamp(day: date, at: time) -> int:
    return int(datetime.combine(day, at).astimezone().timestamp())


def energy_data_request(
    interval_minutes: int, start_date: date, end_date: date | None = None
) -> dict:
    """The request params `get_energy_data` expects, on local-day boundaries.

    Hourly data covers a range of days, so it needs both ends. Daily and monthly
    buckets are selected by a single timestamp: the device decides how far the
    period reaches from the interval alone.
    """
    start = _local_timestamp(start_date, time(0, 0, 0))
    if interval_minutes == ENERGY_INTERVALS["hourly"]:
        return {
            "start_timestamp": start,
            "end_timestamp": _local_timestamp(end_date or start_date, time(23, 59, 59)),
            "interval": interval_minutes,
        }
    return {
        "start_timestamp": start,
        "end_timestamp": start,
        "interval": interval_minutes,
    }


# ---------------------------------------------------------------------------
# One configured device
# ---------------------------------------------------------------------------


class TapoDevice:
    """A configured device and its cached connection.

    The connection is established on first use and then kept. A failure to
    connect at startup is not fatal: the next request tries again.
    """

    def __init__(self, entry: DeviceEntry, credentials: Credentials) -> None:
        self.entry = entry
        self._credentials = credentials
        self._client: Device | None = None
        self._client_config: DeviceConfig | None = None
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def device_type(self) -> str:
        return self.entry.device_type

    async def client(self) -> Device:
        """The cached connection, establishing it first if there isn't one."""
        if self._client is not None:
            return self._client

        async with self._lock:
            if self._client is not None:
                return self._client
            self._client = await self._connect()
            return self._client

    async def _connect(self) -> Device:
        host = self.entry.ip_addr
        try:
            if self._client_config is not None:
                # We have probed this device before; go straight in.
                return await asyncio.wait_for(
                    Device.connect(config=self._client_config),
                    timeout=CONNECT_TIMEOUT_SECONDS,
                )
            device = await asyncio.wait_for(
                Discover.try_connect_all(host, credentials=self._credentials),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self._client_config = None
            raise DeviceError(
                f"Failed to connect to {self.device_type} '{self.name}': timed out"
            ) from None
        except Exception as err:
            self._client_config = None
            raise DeviceError(
                f"Failed to connect to {self.device_type} '{self.name}': {err}"
            ) from err

        if device is None:
            raise DeviceError(
                f"Failed to connect to {self.device_type} '{self.name}': "
                f"no supported protocol answered at {host}"
            )

        self._client_config = device.config
        logger.debug("Established a connection with device '%s'!", self.name)
        return device

    async def connect(self) -> None:
        """Warm the connection, used at startup."""
        await self.client()

    async def refresh_session(self) -> None:
        """Drop the cached connection and hand-shake again."""
        async with self._lock:
            await self._close()
            self._client = await self._connect()

    async def disconnect(self) -> None:
        async with self._lock:
            await self._close()

    async def _close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as err:  # pragma: no cover - best effort teardown
            logger.debug("Ignoring error while disconnecting '%s': %s", self.name, err)

    async def raw(self, method: str, params: dict | None = None) -> dict:
        """Send one native request and return its (normalised) body.

        Tapo devices expire their sessions after a while, so a failed request is
        retried once on a fresh connection rather than surfacing to the caller.
        """
        try:
            response = await self._query(method, params)
        except Exception as first_error:
            logger.info(
                "Retrying %s on '%s' with a fresh connection after: %s",
                method,
                self.name,
                first_error,
            )
            await self.disconnect()
            try:
                response = await self._query(method, params)
            except Exception as err:
                await self.disconnect()
                raise DeviceError(f"{method} failed on '{self.name}': {err}") from err

        body = response.get(method) if isinstance(response, dict) else None
        return normalise_payload(body if body is not None else {})

    async def _query(self, method: str, params: dict | None) -> dict:
        client = await self.client()
        return await client.protocol.query({method: params})

    async def update(self) -> Device:
        """A full python-kasa refresh, needed before touching modules."""
        client = await self.client()
        try:
            await client.update()
        except Exception as err:
            await self.disconnect()
            raise DeviceError(f"Failed to update '{self.name}': {err}") from err
        return client

    async def module(self, module_name: str):
        """A python-kasa module, or `ActionError` if this device lacks it.

        This is what replaces a hand-maintained table of model capabilities: the
        device itself reports which features it carries.
        """
        client = await self.update()
        module = client.modules.get(module_name)
        if module is None:
            raise ActionError(
                f"Device '{self.name}' ({self.device_type}) does not support "
                f"the '{module_name}' feature"
            )
        return module


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


async def set_power(device: TapoDevice, on: bool) -> None:
    await device.raw("set_device_info", {"device_on": on})


async def set_brightness(device: TapoDevice, brightness: int) -> None:
    light = await device.module(Module.Light)
    await light.set_brightness(brightness)


async def set_color_temp(device: TapoDevice, kelvin: int) -> None:
    light = await device.module(Module.Light)
    await light.set_color_temp(kelvin)


async def set_hue_saturation(device: TapoDevice, hue: int, saturation: int) -> None:
    light = await device.module(Module.Light)
    await light.set_hsv(hue, saturation)


async def set_light_effect(device: TapoDevice, effect: str) -> str:
    """Select a lighting effect by name, matched against what the strip offers."""
    effects = await device.module(Module.LightEffect)
    available = list(effects.effect_list or [])
    match = next((name for name in available if name.lower() == effect.lower()), None)
    if match is None:
        raise ActionError(
            f"Device '{device.name}' does not offer the '{effect}' effect. "
            f"Available: {', '.join(available) or 'none'}"
        )
    await effects.set_effect(match)
    return match


async def energy_history(
    device: TapoDevice,
    interval_minutes: int,
    start_date: date,
    end_date: date | None = None,
) -> dict:
    """The device's own `get_energy_data` payload for one interval."""
    request = energy_data_request(interval_minutes, start_date, end_date)
    return await device.raw("get_energy_data", request)


async def child_devices(device: TapoDevice) -> list:
    """Every outlet of a power strip, paging through the device's list."""
    children: list[dict] = []
    start_index = 0
    while True:
        body = await device.raw("get_child_device_list", {"start_index": start_index})
        page = body.get("child_device_list") or []
        children.extend(page)
        total = body.get("sum")
        if not page or total is None or len(children) >= total:
            break
        start_index = len(children)
    return children


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class DeviceRegistry:
    """All configured devices, keyed by name."""

    devices: dict[str, TapoDevice] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Index the devices by slug as well, for names a URL cannot carry."""
        candidates: dict[str, list[TapoDevice]] = {}
        for device in self.devices.values():
            slug = device.entry.slug
            # Never let a slug shadow a device whose real name it matches.
            if slug and slug not in self.devices:
                candidates.setdefault(slug, []).append(device)

        self._by_slug: dict[str, TapoDevice] = {}
        for slug, matches in candidates.items():
            if len(matches) > 1:
                logger.warning(
                    "Devices %s all reduce to the slug '%s'; address them by "
                    "their exact name instead.",
                    ", ".join(f"'{device.name}'" for device in matches),
                    slug,
                )
                continue
            self._by_slug[slug] = matches[0]

    @classmethod
    def from_config(cls, config: Config) -> "DeviceRegistry":
        credentials = Credentials(config.email, config.password)
        return cls(
            devices={
                entry.name: TapoDevice(entry, credentials) for entry in config.devices
            }
        )

    def get(self, name: str) -> TapoDevice | None:
        """A device by its exact name, or failing that by its slug."""
        device = self.devices.get(name)
        if device is not None:
            return device
        return self._by_slug.get(name)

    def conn_infos(self) -> list[dict]:
        return [device.entry.conn_infos() for device in self.devices.values()]

    async def connect_all(self) -> None:
        """Connect to everything at once. Failures are logged, never fatal."""
        if not self.devices:
            return
        logger.info(
            "Attempting to connect to the %d configured device(s)...", len(self.devices)
        )
        results = await asyncio.gather(
            *(device.connect() for device in self.devices.values()),
            return_exceptions=True,
        )
        for device, result in zip(self.devices.values(), results):
            if isinstance(result, BaseException):
                logger.error("! Failed to connect to device '%s': %s", device.name, result)
            else:
                logger.info("|> Device %s connected successfully!", device.name)

    async def disconnect_all(self) -> None:
        await asyncio.gather(
            *(device.disconnect() for device in self.devices.values()),
            return_exceptions=True,
        )
