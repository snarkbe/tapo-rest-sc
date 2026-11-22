"""`/get_all_sensor_data` -- every sensor paired with a configured hub.

The sibling of `/get_all_device_power`, built for the same Homepage
`customapi` widget: unauthenticated, a flat JSON array, Flask-style
serialisation. One entry per sensor, in hub order then in the order the hub
lists its children.

A hub answers `get_child_device_list` with the latest reading of every paired
sensor, so one request per hub covers them all -- the sensors themselves are
asleep on sub-GHz radio and are never contacted directly.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request, Response

from tapo_devices import TapoDevice, child_devices
from tapo_power import DEVICE_TIMEOUT_SECONDS, flask_json_response

logger = logging.getLogger(__name__)

router = APIRouter()

_UNIT_SYMBOLS = {"celsius": "°C", "fahrenheit": "°F"}


def _climate(child: dict, data: dict) -> list[str]:
    """T310/T315: temperature and humidity, plus the app's comfort-range flags."""
    parts: list[str] = []
    if "current_temp" in child:
        unit = child.get("temp_unit") or "celsius"
        data["temperature"] = child["current_temp"]
        data["temperature_unit"] = unit
        # Non-zero when outside the comfort range set in the Tapo app -- what
        # turns the reading red there.
        data["temperature_warning"] = child.get("current_temp_exception", 0) != 0
        parts.append(f"{child['current_temp']}{_UNIT_SYMBOLS.get(unit, '')}")
    if "current_humidity" in child:
        data["humidity"] = child["current_humidity"]
        data["humidity_warning"] = child.get("current_humidity_exception", 0) != 0
        parts.append(f"{child['current_humidity']}%")
    return parts


def _water_leak(child: dict, data: dict) -> list[str]:
    """T300: `water_leak_status` is normal, water_leak or water_dry."""
    if "water_leak_status" not in child:
        return []
    leaking = child["water_leak_status"] == "water_leak" or bool(child.get("in_alarm"))
    data["water_leak"] = leaking
    data["water_leak_status"] = child["water_leak_status"]
    return ["Leak!" if leaking else "Dry"]


def _contact(child: dict, data: dict) -> list[str]:
    """T110."""
    if "open" not in child:
        return []
    data["open"] = bool(child["open"])
    return ["Open" if child["open"] else "Closed"]


def _motion(child: dict, data: dict) -> list[str]:
    """T100."""
    if "detected" not in child:
        return []
    data["motion"] = bool(child["detected"])
    return ["Motion" if child["detected"] else "Clear"]


_READERS = (_climate, _water_leak, _contact, _motion)


def shape_sensor(hub: TapoDevice, child: dict) -> dict:
    """One child of a hub, in the per-entry shape the response uses.

    Fields appear only when the sensor reports them, so a leak sensor carries no
    `temperature`. `summary` strings them together ("20.8°C | 41%") for widgets
    that can show a single label.
    """
    data: dict = {}
    parts: list[str] = []
    for reader in _READERS:
        parts.extend(reader(child, data))

    if "at_low_battery" in child:
        data["low_battery"] = bool(child["at_low_battery"])
    if "battery_percentage" in child:
        data["battery_percentage"] = child["battery_percentage"]
    # A sensor that stopped reporting keeps its last reading in the hub; say so.
    data["online"] = child.get("status", "online") == "online"
    if not parts:
        parts.append("Online" if data["online"] else "Offline")
    data["summary"] = " | ".join(parts)

    return {
        "device": child.get("nickname") or child.get("model") or child.get("device_id"),
        "hub": hub.name,
        "model": child.get("model"),
        "status": "success",
        "data": data,
    }


async def _fetch_sensors(hub: TapoDevice) -> list[dict]:
    """Every sensor on one hub, or a single failed entry naming the hub."""
    try:
        children = await asyncio.wait_for(
            child_devices(hub), timeout=DEVICE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.error("Timed out listing sensors on hub %s", hub.name)
        return [
            {
                "device": hub.name,
                "hub": hub.name,
                "error": f"Request failed: timed out after {DEVICE_TIMEOUT_SECONDS}s",
                "status": "failed",
            }
        ]
    except Exception as err:
        logger.error("Failed to list sensors on hub %s: %s", hub.name, err)
        return [
            {
                "device": hub.name,
                "hub": hub.name,
                "error": f"Request failed: {type(err).__name__}",
                "details": str(err)[:200],
                "status": "failed",
            }
        ]
    logger.debug("Sensors on hub %s: %s", hub.name, children)
    return [shape_sensor(hub, child) for child in children if isinstance(child, dict)]


async def collect_sensors(service) -> list[dict]:
    """Poll every configured hub concurrently and flatten their sensors."""
    config = service.config
    hubs = [
        hub
        for entry in (config.hubs if config else ())
        if (hub := service.registry.get(entry.name)) is not None
    ]
    per_hub = await asyncio.gather(*(_fetch_sensors(hub) for hub in hubs))
    return [entry for entries in per_hub for entry in entries]


@router.get("/get_all_sensor_data", include_in_schema=False)
async def get_all_sensor_data(request: Request) -> Response:
    """Readings of every sensor paired with a configured hub."""
    service = request.app.state.service

    if service.init_error:
        logger.error("API call failed due to initialization error: %s", service.init_error)
        return flask_json_response(
            {
                "error": "Server initialization failed. Check logs.",
                "details": service.init_error,
            },
            status_code=500,
        )

    return flask_json_response(await collect_sensors(service))
