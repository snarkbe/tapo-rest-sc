"""`/get_all_device_power` -- the aggregated reading the Homepage widget consumes.

The response contract is deliberately frozen: a top-level JSON array, one entry
per configured device in configuration order, each with `device`, `status` and
`data.current_power`, optional `substract` chaining, and a synthetic
`Total Consumption` entry appended last.

Serialisation matches Flask's `jsonify` byte for byte (sorted keys, ASCII
escaping, trailing newline) so nothing downstream sees a difference.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from tapo_config import InvalidDeviceEntry
from tapo_devices import TapoDevice

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-device budget, matching the timeout the HTTP hop used to impose.
DEVICE_TIMEOUT_SECONDS = 10

TOTAL_DEVICE_NAME = "Total Consumption"


def flask_json_response(payload, status_code: int = 200) -> Response:
    """Render exactly as Flask's jsonify did, to keep the payload byte-identical."""
    body = json.dumps(payload, sort_keys=True, ensure_ascii=True) + "\n"
    return Response(content=body, status_code=status_code, media_type="application/json")


def _extract_power(data: dict):
    """Find `current_power`, tolerating a nested `result` wrapper as before."""
    if not isinstance(data, dict):
        return None, None
    if isinstance(data.get("result"), dict) and "current_power" in data["result"]:
        return data["result"]["current_power"], "result"
    if "current_power" in data:
        return data["current_power"], "flat"
    return None, None


async def _fetch_power(device: TapoDevice) -> dict:
    """One device's current power, in the per-entry shape the response uses."""
    try:
        data = await asyncio.wait_for(
            device.raw("get_current_power"), timeout=DEVICE_TIMEOUT_SECONDS
        )
        logger.debug("Power reading for %s: %s", device.name, data)
        return {"device": device.name, "data": data, "status": "success"}
    except asyncio.TimeoutError:
        logger.error("Timed out reading power from %s", device.name)
        return {
            "device": device.name,
            "error": f"Request failed: timed out after {DEVICE_TIMEOUT_SECONDS}s",
            "status": "failed",
        }
    except Exception as err:
        logger.error("Failed to read power from %s: %s", device.name, err)
        return {
            "device": device.name,
            "error": f"Request failed: {type(err).__name__}",
            "details": str(err)[:200],
            "status": "failed",
        }


def _apply_subtractions(items, results: list[dict]) -> None:
    """Subtract chained devices in place, exactly as the Flask version did."""
    results_by_name = {
        result.get("device"): result for result in results if result.get("device")
    }

    for item in items:
        if isinstance(item, InvalidDeviceEntry):
            continue

        device_name = item.name
        subtract_device_name = item.substract
        if not subtract_device_name or device_name not in results_by_name:
            continue

        main_result = results_by_name[device_name]
        if main_result.get("status") != "success" or "data" not in main_result:
            logger.warning(
                "Cannot apply subtraction for '%s': No valid power data available",
                device_name,
            )
            continue

        if subtract_device_name not in results_by_name:
            logger.warning(
                "Cannot apply subtraction for '%s': Device '%s' not found",
                device_name,
                subtract_device_name,
            )
            main_result["data"]["subtraction_error"] = (
                f"Device to subtract '{subtract_device_name}' not found"
            )
            continue

        subtract_result = results_by_name[subtract_device_name]
        if subtract_result.get("status") != "success" or "data" not in subtract_result:
            logger.warning(
                "Cannot apply subtraction for '%s': No valid power data for '%s'",
                device_name,
                subtract_device_name,
            )
            main_result["data"]["subtraction_error"] = (
                f"No valid power data for '{subtract_device_name}'"
            )
            continue

        try:
            main_data = main_result.get("data", {})
            subtract_data = subtract_result.get("data", {})

            main_power, main_location = _extract_power(main_data)
            subtract_power, _ = _extract_power(subtract_data)

            if main_power is None or subtract_power is None:
                raise KeyError(
                    "Could not find current_power in the data. "
                    f"Main power found: {main_power is not None}, "
                    f"Subtract power found: {subtract_power is not None}"
                )

            if isinstance(main_power, (int, float)) and isinstance(
                subtract_power, (int, float)
            ):
                original_power = main_power
                # Never report negative consumption.
                adjusted_power = max(0, main_power - subtract_power)

                if main_location == "result":
                    main_result["data"]["result"]["current_power"] = adjusted_power
                else:
                    main_result["data"]["current_power"] = adjusted_power

                main_result["data"]["subtraction_info"] = {
                    "original_power": original_power,
                    "subtracted_device": subtract_device_name,
                    "subtracted_power": subtract_power,
                    "adjusted_power": adjusted_power,
                }
            else:
                logger.warning(
                    "Cannot apply subtraction for '%s': Power values not numeric",
                    device_name,
                )
                main_result["data"]["subtraction_error"] = "Power values not numeric"
        except KeyError as err:
            logger.warning(
                "Cannot apply subtraction for '%s': Missing power data fields - %s",
                device_name,
                err,
            )
            main_result["data"]["subtraction_error"] = f"Missing power data fields: {err}"
        except Exception as err:
            logger.error(
                "Error applying subtraction for '%s': %s", device_name, err, exc_info=True
            )
            main_result["data"]["subtraction_error"] = f"Error during subtraction: {err}"


def _total_entry(results: list[dict]) -> dict:
    """Sum the adjusted readings; appended last so the widget shows it as a footer."""
    total_power = 0
    total_power_devices = []

    for result in results:
        if result.get("status") == "success" and "data" in result:
            power_value, _ = _extract_power(result.get("data", {}))
            if isinstance(power_value, (int, float)):
                total_power += power_value
                total_power_devices.append(result.get("device"))

    return {
        "device": TOTAL_DEVICE_NAME,
        "status": "success",
        "data": {
            "current_power": total_power,
            "included_devices": total_power_devices,
        },
    }


async def collect_power(service) -> list[dict]:
    """Poll every configured device concurrently and build the response array."""
    config = service.config
    items = config.items if config else ()

    results: list[dict] = []
    pending: list[tuple[int, TapoDevice]] = []

    for item in items:
        if isinstance(item, InvalidDeviceEntry):
            results.append(
                {"device_info": item.raw, "error": item.reason, "status": "skipped"}
            )
            continue
        device = service.registry.get(item.name)
        if device is None:  # pragma: no cover - registry is built from these entries
            results.append(
                {
                    "device": item.name,
                    "error": "Device is not registered",
                    "status": "failed",
                }
            )
            continue
        pending.append((len(results), device))
        results.append({})

    if pending:
        fetched = await asyncio.gather(
            *(_fetch_power(device) for _, device in pending)
        )
        for (index, _), result in zip(pending, fetched):
            results[index] = result

    _apply_subtractions(items, results)
    results.append(_total_entry(results))
    return results


@router.get("/get_all_device_power", include_in_schema=False)
async def get_all_device_power(request: Request) -> Response:
    """Power for every configured device, in one response, plus the total."""
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

    return flask_json_response(await collect_power(service))


@router.get("/", include_in_schema=False)
async def default_route() -> Response:
    return RedirectResponse(url="/get_all_device_power", status_code=302)
