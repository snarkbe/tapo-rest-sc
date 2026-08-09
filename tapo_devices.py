"""Talking to Tapo devices with python-kasa.

This is the replacement for the bundled `tapo-rest` Rust binary. It keeps the
same shape as tapo-rest's device layer: one object per configured device,
holding a lazily-established and then cached connection, plus a table of which
actions each device model supports.

Payloads are the device's own JSON, so the field names match what tapo-rest
returned. Two normalisations are applied on top, mirroring what the `tapo`
crate does: `ssid`/`nickname` are base64-decoded, and `local_time` is rendered
in ISO form. `get_energy_data` is reshaped into tapo-rest's
`{local_time, start_date_time, entries, interval_length}`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable

from kasa import Credentials, Device, DeviceConfig, Discover, Module

from tapo_config import Config, DeviceEntry

logger = logging.getLogger(__name__)

# How long to wait for the initial protocol probe of a single device.
CONNECT_TIMEOUT_SECONDS = 30


class DeviceError(Exception):
    """A device could not be reached, or refused a command."""


class ActionError(Exception):
    """The caller asked for something this device cannot do (a 400, not a 500)."""


# ---------------------------------------------------------------------------
# Preset colours, ported from the tapo crate (hue, saturation, colour temp).
# A non-zero colour temperature means the preset is a white, and the hue and
# saturation are ignored.
# ---------------------------------------------------------------------------

COLOR_PRESETS: dict[str, tuple[int, int, int]] = {
    "CoolWhite": (0, 100, 4000),
    "Daylight": (0, 100, 5000),
    "Ivory": (0, 100, 6000),
    "WarmWhite": (0, 100, 3000),
    "Incandescent": (0, 100, 2700),
    "Candlelight": (0, 100, 2500),
    "Snow": (0, 100, 6500),
    "GhostWhite": (0, 100, 6500),
    "AliceBlue": (208, 5, 0),
    "LightGoldenrod": (54, 28, 0),
    "LemonChiffon": (54, 19, 0),
    "AntiqueWhite": (0, 100, 5500),
    "Gold": (50, 100, 0),
    "Peru": (29, 69, 0),
    "Chocolate": (30, 100, 0),
    "SandyBrown": (27, 60, 0),
    "Coral": (16, 68, 0),
    "Pumpkin": (24, 90, 0),
    "Tomato": (9, 72, 0),
    "Vermilion": (4, 77, 0),
    "OrangeRed": (16, 100, 0),
    "Pink": (349, 24, 0),
    "Crimson": (348, 90, 0),
    "DarkRed": (0, 100, 0),
    "HotPink": (330, 58, 0),
    "Smitten": (329, 67, 0),
    "MediumPurple": (259, 48, 0),
    "BlueViolet": (271, 80, 0),
    "Indigo": (274, 100, 0),
    "LightSkyBlue": (202, 46, 0),
    "CornflowerBlue": (218, 57, 0),
    "Ultramarine": (254, 100, 0),
    "DeepSkyBlue": (195, 100, 0),
    "Azure": (210, 100, 0),
    "NavyBlue": (240, 100, 0),
    "LightTurquoise": (180, 26, 0),
    "Aquamarine": (159, 50, 0),
    "Turquoise": (174, 71, 0),
    "LightGreen": (120, 39, 0),
    "Lime": (75, 100, 0),
    "ForestGreen": (120, 75, 0),
}

# Lighting effect presets tapo-rest accepts. They are matched against the
# effect names the light strip itself reports.
LIGHTING_EFFECT_PRESETS = (
    "Aurora", "BubblingCauldron", "CandyCane", "Christmas", "Flicker",
    "GrandmasChristmasLights", "Hanukkah", "HauntedMansion", "Icicle",
    "Lightning", "Ocean", "Rainbow", "Raindrop", "Spring", "Sunrise",
    "Sunset", "Valentines",
)  # fmt: skip


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
    """Devices report `2026-08-09 18:29:18`; the tapo crate renders it ISO."""
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


def _utc_iso(moment: datetime) -> str:
    """Render as chrono renders a DateTime<Utc>: RFC 3339, whole seconds, Z."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_timestamp(day: date, at: time) -> int:
    return int(datetime.combine(day, at).astimezone().timestamp())


def _add_months(moment: datetime, months: int) -> datetime:
    """Calendar-month arithmetic, clamping the day like chrono's Months does."""
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def build_energy_data_request(
    interval_minutes: int, start_date: date, end_date: date | None = None
) -> dict:
    """The request params the tapo crate builds for each EnergyDataInterval."""
    if interval_minutes == 60:
        return {
            "start_timestamp": _local_timestamp(start_date, time(0, 0, 0)),
            "end_timestamp": _local_timestamp(end_date or start_date, time(23, 59, 59)),
            "interval": 60,
        }
    stamp = _local_timestamp(start_date, time(0, 0, 0))
    return {
        "start_timestamp": stamp,
        "end_timestamp": stamp,
        "interval": interval_minutes,
    }


def shape_energy_data(raw: dict) -> dict:
    """Turn the device's `{data, start_timestamp, interval}` into tapo-rest's shape."""
    interval = raw.get("interval")
    start_timestamp = raw.get("start_timestamp")
    if interval is None or start_timestamp is None:
        raise DeviceError("Device returned energy data without an interval")

    cursor = datetime.fromtimestamp(start_timestamp).astimezone()
    start_date_time = _utc_iso(cursor)

    entries = []
    for energy in raw.get("data") or []:
        entries.append({"start_date_time": _utc_iso(cursor), "energy": energy})
        if interval == 60:
            cursor = cursor + timedelta(hours=1)
        elif interval == 1440:
            cursor = cursor + timedelta(days=1)
        elif interval == 43200:
            cursor = _add_months(cursor.replace(tzinfo=None), 1).astimezone()
        else:
            raise DeviceError(f"Unsupported interval duration: {interval} minutes")

    return {
        "local_time": _normalise_local_time(raw.get("local_time")),
        "start_date_time": start_date_time,
        "entries": entries,
        "interval_length": interval,
    }


# ---------------------------------------------------------------------------
# One configured device
# ---------------------------------------------------------------------------


class TapoDevice:
    """A configured device and its cached connection.

    The connection is established on first use and then kept, exactly as
    tapo-rest did. A failure to connect at startup is not fatal: the next
    request tries again.
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
        """Drop the cached connection and hand-shake again.

        python-kasa has no explicit session-refresh call; re-authenticating is
        the equivalent of tapo-rest's `refresh_session`.
        """
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

        Tapo devices expire their sessions after a while. Rather than failing
        the request the way tapo-rest did -- it answered `Session timeout` until
        someone called `/refresh-session` -- the connection is dropped and
        re-established once, transparently.
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
        client = await self.update()
        module = client.modules.get(module_name)
        if module is None:
            raise ActionError(
                f"Device '{self.name}' ({self.device_type}) does not support "
                f"the '{module_name}' feature"
            )
        return module


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    name: str
    kind: str  # u8 | u16 | date | color | effect
    required: bool = True


@dataclass(frozen=True)
class Action:
    """One action, and the URI segment it is exposed under."""

    name: str
    handler: Callable[["TapoDevice", dict], Awaitable[Any]]
    params: tuple[ParamSpec, ...] = ()

    @property
    def uri_segment(self) -> str:
        return self.name.replace("_", "-")


async def _act_on(device: TapoDevice, _params: dict) -> None:
    await device.raw("set_device_info", {"device_on": True})


async def _act_off(device: TapoDevice, _params: dict) -> None:
    await device.raw("set_device_info", {"device_on": False})


async def _act_set_brightness(device: TapoDevice, params: dict) -> None:
    light = await device.module(Module.Light)
    await light.set_brightness(params["level"])


async def _act_set_color(device: TapoDevice, params: dict) -> None:
    hue, saturation, color_temp = COLOR_PRESETS[params["color"]]
    light = await device.module(Module.Light)
    if color_temp:
        await light.set_color_temp(color_temp)
    else:
        await light.set_hsv(hue, saturation)


async def _act_set_hue_saturation(device: TapoDevice, params: dict) -> None:
    light = await device.module(Module.Light)
    await light.set_hsv(params["hue"], params["saturation"])


async def _act_set_color_temperature(device: TapoDevice, params: dict) -> None:
    light = await device.module(Module.Light)
    await light.set_color_temp(params["color_temperature"])


async def _act_set_lighting_effect(device: TapoDevice, params: dict) -> None:
    effects = await device.module(Module.LightEffect)
    wanted = params["lighting_effect"]
    available = list(effects.effect_list or [])
    match = next((name for name in available if name.lower() == wanted.lower()), None)
    if match is None:
        # Tapo's preset names and the strip's own scene names do not always agree.
        raise ActionError(
            f"Device '{device.name}' does not offer the '{wanted}' effect. "
            f"Available: {', '.join(available) or 'none'}"
        )
    await effects.set_effect(match)


async def _act_get_device_info(device: TapoDevice, _params: dict) -> dict:
    return await device.raw("get_device_info")


async def _act_get_device_usage(device: TapoDevice, _params: dict) -> dict:
    return await device.raw("get_device_usage")


async def _act_get_energy_usage(device: TapoDevice, _params: dict) -> dict:
    return await device.raw("get_energy_usage")


async def _act_get_current_power(device: TapoDevice, _params: dict) -> dict:
    return await device.raw("get_current_power")


async def _energy_data(device: TapoDevice, interval: int, params: dict) -> dict:
    request = build_energy_data_request(
        interval, params["start_date"], params.get("end_date")
    )
    return shape_energy_data(await device.raw("get_energy_data", request))


async def _act_get_hourly_energy_data(device: TapoDevice, params: dict) -> dict:
    return await _energy_data(device, 60, params)


async def _act_get_daily_energy_data(device: TapoDevice, params: dict) -> dict:
    return await _energy_data(device, 1440, params)


async def _act_get_monthly_energy_data(device: TapoDevice, params: dict) -> dict:
    return await _energy_data(device, 43200, params)


async def _act_get_child_device_list(device: TapoDevice, _params: dict) -> list:
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


_ON_OFF = (
    Action("on", _act_on),
    Action("off", _act_off),
)
_INFO = Action("get_device_info", _act_get_device_info)
_USAGE = Action("get_device_usage", _act_get_device_usage)
_BRIGHTNESS = Action(
    "set_brightness", _act_set_brightness, (ParamSpec("level", "u8"),)
)
_COLOR_ACTIONS = (
    Action("set_color", _act_set_color, (ParamSpec("color", "color"),)),
    Action(
        "set_hue_saturation",
        _act_set_hue_saturation,
        (ParamSpec("hue", "u16"), ParamSpec("saturation", "u8")),
    ),
    Action(
        "set_color_temperature",
        _act_set_color_temperature,
        (ParamSpec("color_temperature", "u16"),),
    ),
)
_ENERGY_ACTIONS = (
    Action("get_energy_usage", _act_get_energy_usage),
    Action(
        "get_hourly_energy_data",
        _act_get_hourly_energy_data,
        (ParamSpec("start_date", "date"), ParamSpec("end_date", "date", required=False)),
    ),
    Action(
        "get_daily_energy_data",
        _act_get_daily_energy_data,
        (ParamSpec("start_date", "date"),),
    ),
    Action(
        "get_monthly_energy_data",
        _act_get_monthly_energy_data,
        (ParamSpec("start_date", "date"),),
    ),
    Action("get_current_power", _act_get_current_power),
)


@dataclass(frozen=True)
class DeviceGroup:
    """Models that share a handler type, and therefore an action set."""

    models: tuple[str, ...]
    description: str
    actions: tuple[Action, ...]

    def action(self, uri_segment: str) -> Action | None:
        return next((a for a in self.actions if a.uri_segment == uri_segment), None)


DEVICE_GROUPS: tuple[DeviceGroup, ...] = (
    DeviceGroup(("L510", "L520", "L610"), "bulb", (*_ON_OFF, _BRIGHTNESS, _INFO, _USAGE)),
    DeviceGroup(
        ("L530", "L535", "L630"),
        "bulb",
        (*_ON_OFF, _BRIGHTNESS, *_COLOR_ACTIONS, _INFO, _USAGE),
    ),
    DeviceGroup(
        ("L900",), "light strip", (*_ON_OFF, _BRIGHTNESS, *_COLOR_ACTIONS, _INFO, _USAGE)
    ),
    DeviceGroup(
        ("L920", "L930"),
        "light strip",
        (
            *_ON_OFF,
            _BRIGHTNESS,
            *_COLOR_ACTIONS,
            Action(
                "set_lighting_effect",
                _act_set_lighting_effect,
                (ParamSpec("lighting_effect", "effect"),),
            ),
            _INFO,
            _USAGE,
        ),
    ),
    DeviceGroup(("P100", "P105"), "plug", (*_ON_OFF, _INFO, _USAGE)),
    DeviceGroup(
        ("P110", "P110M", "P115"), "plug", (*_ON_OFF, _INFO, _USAGE, *_ENERGY_ACTIONS)
    ),
    DeviceGroup(
        ("P300",),
        "power strip",
        (_INFO, Action("get_child_device_list", _act_get_child_device_list)),
    ),
    DeviceGroup(
        ("P304", "P304M", "P316"),
        "energy monitoring power strip",
        (_INFO, Action("get_child_device_list", _act_get_child_device_list)),
    ),
)

GROUP_BY_MODEL: dict[str, DeviceGroup] = {
    model: group for group in DEVICE_GROUPS for model in group.models
}


def action_uris() -> list[str]:
    """Every action route, in the order tapo-rest listed them on `/actions`."""
    return [
        f"/{model.lower()}/{action.uri_segment}"
        for group in DEVICE_GROUPS
        for model in group.models
        for action in group.actions
    ]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class DeviceRegistry:
    """All configured devices, keyed by name."""

    devices: dict[str, TapoDevice] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: Config) -> "DeviceRegistry":
        credentials = Credentials(config.email, config.password)
        return cls(
            devices={
                entry.name: TapoDevice(entry, credentials) for entry in config.devices
            }
        )

    def get(self, name: str) -> TapoDevice | None:
        return self.devices.get(name)

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
