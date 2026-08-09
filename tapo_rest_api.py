"""The tapo-rest compatible REST surface.

Routes are generated from the device-group table in `tapo_devices`, the way
tapo-rest generated them from its `build_router!` macro: one GET route per
(model, action) pair at `/actions/<model>/<action-with-dashes>`, with the
device picked by a mandatory `?device=<name>` query parameter.

Auth is a static API key in `Authorization: Bearer <key>`. Errors are plain
text, never JSON, matching tapo-rest.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from tapo_devices import (
    COLOR_PRESETS,
    DEVICE_GROUPS,
    LIGHTING_EFFECT_PRESETS,
    Action,
    ActionError,
    DeviceError,
    DeviceGroup,
    ParamSpec,
    action_uris,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class ApiError(Exception):
    """An error with the status code and plain-text body tapo-rest would return."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


async def api_error_handler(_request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return PlainTextResponse(exc.message, status_code=exc.status_code)


async def device_error_handler(_request: Request, exc: Exception) -> Response:
    """A device refused or could not be reached: tapo-rest reported these as 500."""
    return PlainTextResponse(str(exc), status_code=500)


async def action_error_handler(_request: Request, exc: Exception) -> Response:
    """The caller asked for something the device cannot do: a 400."""
    return PlainTextResponse(str(exc), status_code=400)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def require_api_key(request: Request) -> None:
    """Reject anything without a configured API key in the Authorization header."""
    header = request.headers.get("authorization")
    if not header:
        raise ApiError(400, "Header of type `authorization` was missing")

    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(400, "Header of type `authorization` was malformed")

    service = request.app.state.service
    config = service.config
    if config is None or not config.is_valid_api_key(token.strip()):
        # Deliberately not logging the rejected key.
        logger.error("Rejected a request carrying an invalid API key (bearer token)")
        raise ApiError(403, "Invalid bearer token")


# ---------------------------------------------------------------------------
# Query parameter parsing
# ---------------------------------------------------------------------------


def _missing(field: str) -> ApiError:
    return ApiError(400, f"Failed to deserialize query string: missing field `{field}`")


def _invalid(field: str, detail: str) -> ApiError:
    return ApiError(400, f"Failed to deserialize query string: {field}: {detail}")


def _parse_int(field: str, value: str, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise _invalid(field, f"invalid digit found in string: '{value}'") from None
    if not 0 <= parsed <= maximum:
        raise _invalid(field, f"number out of range (0..={maximum})")
    return parsed


def _parse_date(field: str, value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise _invalid(
            field, f"expected a date in YYYY-MM-DD format, got '{value}'"
        ) from None


def parse_params(specs: tuple[ParamSpec, ...], query: Any) -> dict:
    parsed: dict = {}
    for spec in specs:
        raw = query.get(spec.name)
        if raw is None or raw == "":
            if spec.required:
                raise _missing(spec.name)
            continue

        if spec.kind == "u8":
            parsed[spec.name] = _parse_int(spec.name, raw, 255)
        elif spec.kind == "u16":
            parsed[spec.name] = _parse_int(spec.name, raw, 65535)
        elif spec.kind == "date":
            parsed[spec.name] = _parse_date(spec.name, raw)
        elif spec.kind == "color":
            if raw not in COLOR_PRESETS:
                raise _invalid(
                    spec.name,
                    f"unknown colour '{raw}'. Known: {', '.join(sorted(COLOR_PRESETS))}",
                )
            parsed[spec.name] = raw
        elif spec.kind == "effect":
            if raw not in LIGHTING_EFFECT_PRESETS:
                raise _invalid(
                    spec.name,
                    f"unknown lighting effect '{raw}'. "
                    f"Known: {', '.join(LIGHTING_EFFECT_PRESETS)}",
                )
            parsed[spec.name] = raw
        else:  # pragma: no cover - guarded by the action table
            raise _invalid(spec.name, f"unsupported parameter kind '{spec.kind}'")
    return parsed


def _resolve_device(request: Request, name: str | None):
    if not name:
        raise _missing("device")
    device = request.app.state.service.registry.get(name)
    if device is None:
        raise ApiError(404, "Provided device name was not found")
    return device


# ---------------------------------------------------------------------------
# Generated action routes
# ---------------------------------------------------------------------------


def _make_action_endpoint(group: DeviceGroup, action: Action):
    async def endpoint(request: Request) -> Response:
        require_api_key(request)

        device = _resolve_device(request, request.query_params.get("device"))
        if device.device_type not in group.models:
            raise ApiError(
                400,
                f"This route is reserved to {', '.join(group.models)} devices, "
                f"but the provided name refers to a {device.device_type} device",
            )

        params = parse_params(action.params, request.query_params)
        result = await action.handler(device, params)

        if result is None:
            return Response(status_code=200)
        return JSONResponse(result)

    endpoint.__name__ = f"{group.models[0].lower()}_{action.name}"
    return endpoint


for _group in DEVICE_GROUPS:
    for _model in _group.models:
        for _action in _group.actions:
            router.add_api_route(
                f"/actions/{_model.lower()}/{_action.uri_segment}",
                _make_action_endpoint(_group, _action),
                methods=["GET"],
                include_in_schema=False,
            )


# ---------------------------------------------------------------------------
# Top-level routes
# ---------------------------------------------------------------------------

_ACTION_LISTING = "\n".join(action_uris())


@router.get("/actions", include_in_schema=False)
async def list_actions() -> Response:
    """Every available action. Unauthenticated, as in tapo-rest."""
    return PlainTextResponse(_ACTION_LISTING)


@router.get("/devices", include_in_schema=False)
async def list_devices(request: Request) -> Response:
    require_api_key(request)
    return JSONResponse(request.app.state.service.registry.conn_infos())


@router.get("/refresh-session", include_in_schema=False)
async def refresh_session(request: Request) -> Response:
    require_api_key(request)

    name = request.query_params.get("device")
    if not name:
        raise _missing("device")

    device = request.app.state.service.registry.get(name)
    if device is None:
        raise ApiError(404, f"Unknown device: {name}")

    try:
        await device.refresh_session()
    except DeviceError as err:
        logger.error("Failed to refresh the session of '%s': %s", name, err)
        raise ApiError(500, "Failed to refresh device's session") from err

    return Response(status_code=200)


@router.post("/reload-config", include_in_schema=False)
async def reload_config(request: Request) -> Response:
    require_api_key(request)

    try:
        await request.app.state.service.reload()
    except Exception as err:
        logger.error("Failed to reload config: %s", err)
        raise ApiError(500, "Failed to reload config") from err

    return Response(status_code=200)
