"""The device REST API.

Resource-oriented: the device is a path segment, and what it can do is decided
by the device itself rather than by a table of models. Parameters are typed
query parameters, so FastAPI validates them and documents them on `/docs`.

Auth is a static API key in `Authorization: Bearer <key>`. Errors are JSON
`{"detail": ...}`, the shape FastAPI uses everywhere else.
"""

from __future__ import annotations

import logging
from datetime import date
from enum import Enum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from tapo_devices import (
    ENERGY_INTERVALS,
    TapoDevice,
    child_devices,
    energy_history,
    set_brightness,
    set_color_temp,
    set_hue_saturation,
    set_light_effect,
    set_power,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth and device lookup
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(
    auto_error=False,
    description="An API key from `server.api_keys` in the configuration file.",
)


def require_api_key(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Reject anything without a configured API key in the Authorization header."""
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed `Authorization: Bearer <key>` header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    config = request.app.state.service.config
    if config is None or not config.is_valid_api_key(credentials.credentials.strip()):
        # Deliberately not logging the rejected key.
        logger.error("Rejected a request carrying an invalid API key (bearer token)")
        raise HTTPException(status_code=403, detail="Invalid API key")


def get_device(name: str, request: Request) -> TapoDevice:
    device = request.app.state.service.registry.get(name)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Unknown device: {name}")
    return device


DeviceDep = Annotated[TapoDevice, Depends(get_device)]


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


async def device_error_handler(_request: Request, exc: Exception) -> Response:
    """The device could not be reached or refused the command: a bad gateway."""
    return JSONResponse({"detail": str(exc)}, status_code=502)


async def action_error_handler(_request: Request, exc: Exception) -> Response:
    """The caller asked for something the device cannot do: a 400."""
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DeviceInfo(BaseModel):
    """A configured device, as it appears in the configuration file."""

    name: str
    device_type: str
    ip_addr: str


class PowerState(BaseModel):
    name: str
    on: bool


class LightState(BaseModel):
    name: str
    applied: dict[str, Any]


class ReloadResult(BaseModel):
    devices: int


class EnergyInterval(str, Enum):
    hourly = "hourly"
    daily = "daily"
    monthly = "monthly"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("/devices", response_model=list[DeviceInfo], tags=["devices"])
async def list_devices(request: Request):
    """Every configured device."""
    return request.app.state.service.registry.conn_infos()


@router.get("/devices/{name}", tags=["devices"])
async def device_info(device: DeviceDep) -> dict:
    """The device's own `get_device_info` payload."""
    return await device.raw("get_device_info")


@router.get("/devices/{name}/usage", tags=["devices"])
async def device_usage(device: DeviceDep) -> dict:
    """Runtime and power-on statistics."""
    return await device.raw("get_device_usage")


@router.post("/devices/{name}/on", tags=["control"])
async def turn_on(device: DeviceDep) -> PowerState:
    """Switch the device on."""
    await set_power(device, True)
    return PowerState(name=device.name, on=True)


@router.post("/devices/{name}/off", tags=["control"])
async def turn_off(device: DeviceDep) -> PowerState:
    """Switch the device off."""
    await set_power(device, False)
    return PowerState(name=device.name, on=False)


@router.post("/devices/{name}/light", tags=["control"])
async def set_light(
    device: DeviceDep,
    brightness: Annotated[int | None, Query(ge=1, le=100, description="Percent")] = None,
    hue: Annotated[int | None, Query(ge=0, le=360, description="Degrees")] = None,
    saturation: Annotated[int | None, Query(ge=0, le=100, description="Percent")] = None,
    color_temp: Annotated[int | None, Query(ge=1000, le=9000, description="Kelvin")] = None,
    effect: Annotated[str | None, Query(description="A lighting effect name")] = None,
) -> LightState:
    """Apply one or more light settings.

    Anything the device does not support answers 400, naming the missing
    feature. `hue` and `saturation` go together, since they set one colour.

    Settings are applied in order and are not rolled back: if a later one fails,
    the response is a 400 but the earlier ones have already reached the device.
    `applied` lists what actually landed on a success.
    """
    if all(v is None for v in (brightness, hue, saturation, color_temp, effect)):
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of: brightness, hue, saturation, "
            "color_temp, effect",
        )
    if (hue is None) != (saturation is None):
        raise HTTPException(
            status_code=400, detail="`hue` and `saturation` must be given together"
        )

    applied: dict[str, Any] = {}
    if brightness is not None:
        await set_brightness(device, brightness)
        applied["brightness"] = brightness
    if color_temp is not None:
        await set_color_temp(device, color_temp)
        applied["color_temp"] = color_temp
    if hue is not None and saturation is not None:
        await set_hue_saturation(device, hue, saturation)
        applied["hue"] = hue
        applied["saturation"] = saturation
    if effect is not None:
        # The strip's own name for the effect, which may differ in case.
        applied["effect"] = await set_light_effect(device, effect)

    return LightState(name=device.name, applied=applied)


@router.get("/devices/{name}/power", tags=["energy"])
async def current_power(device: DeviceDep) -> dict:
    """The instantaneous reading, in watts."""
    return await device.raw("get_current_power")


@router.get("/devices/{name}/energy", tags=["energy"])
async def energy_usage(device: DeviceDep) -> dict:
    """Cumulative energy counters."""
    return await device.raw("get_energy_usage")


@router.get("/devices/{name}/energy/history", tags=["energy"])
async def energy_history_route(
    device: DeviceDep,
    start_date: Annotated[date, Query(description="YYYY-MM-DD")],
    interval: EnergyInterval = EnergyInterval.daily,
    end_date: Annotated[
        date | None, Query(description="YYYY-MM-DD, hourly only")
    ] = None,
) -> dict:
    """Historic energy data, in hourly, daily or monthly buckets.

    Day boundaries are local ones, so set `TZ` on the container. `end_date` only
    widens an `hourly` range; the other intervals derive their own span.
    """
    return await energy_history(
        device, ENERGY_INTERVALS[interval.value], start_date, end_date
    )


@router.get("/devices/{name}/children", tags=["devices"])
async def list_children(device: DeviceDep) -> list:
    """Every outlet of a power strip."""
    return await child_devices(device)


@router.post("/reload-config", tags=["service"])
async def reload_config(request: Request) -> ReloadResult:
    """Re-read the configuration file without restarting."""
    try:
        await request.app.state.service.reload()
    except Exception as err:
        logger.error("Failed to reload config: %s", err)
        raise HTTPException(
            status_code=500, detail=f"Failed to reload config: {err}"
        ) from err

    config = request.app.state.service.config
    return ReloadResult(devices=len(config.devices) if config else 0)
