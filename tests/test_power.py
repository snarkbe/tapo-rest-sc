"""The /get_all_device_power contract. This is what the Homepage widget reads."""

from __future__ import annotations

import json

PLUGS = [
    {"name": "UPS", "device_type": "P115", "ip_addr": "192.168.0.103"},
    {
        "name": "TV",
        "device_type": "P110",
        "ip_addr": "192.168.0.224",
        "substract": "UPS",
    },
    {"name": "Washer", "device_type": "P115", "ip_addr": "192.168.0.45"},
]


def test_response_is_a_flat_array_in_config_order(make_client):
    client, _ = make_client(
        PLUGS, {"UPS": {"power": 76}, "TV": {"power": 164}, "Washer": {"power": 1}}
    )
    body = client.get("/get_all_device_power").json()

    assert [entry["device"] for entry in body] == [
        "UPS",
        "TV",
        "Washer",
        "Total Consumption",
    ]


def test_subtraction_adjusts_power_and_records_details(make_client):
    client, _ = make_client(
        PLUGS, {"UPS": {"power": 76}, "TV": {"power": 164}, "Washer": {"power": 1}}
    )
    body = client.get("/get_all_device_power").json()
    tv = next(entry for entry in body if entry["device"] == "TV")

    assert tv["data"]["current_power"] == 88
    assert tv["data"]["subtraction_info"] == {
        "original_power": 164,
        "subtracted_device": "UPS",
        "subtracted_power": 76,
        "adjusted_power": 88,
    }


def test_total_sums_adjusted_values_not_raw_ones(make_client):
    client, _ = make_client(
        PLUGS, {"UPS": {"power": 76}, "TV": {"power": 164}, "Washer": {"power": 1}}
    )
    body = client.get("/get_all_device_power").json()
    total = body[-1]

    # 76 + (164 - 76) + 1 -- the chained plug contributes its net figure only.
    assert total == {
        "device": "Total Consumption",
        "status": "success",
        "data": {
            "current_power": 165,
            "included_devices": ["UPS", "TV", "Washer"],
        },
    }


def test_subtraction_never_goes_negative(make_client):
    client, _ = make_client(
        PLUGS, {"UPS": {"power": 200}, "TV": {"power": 20}, "Washer": {"power": 0}}
    )
    body = client.get("/get_all_device_power").json()
    tv = next(entry for entry in body if entry["device"] == "TV")

    assert tv["data"]["current_power"] == 0
    assert tv["data"]["subtraction_info"]["original_power"] == 20


def test_subtraction_target_missing_records_an_error(make_client):
    devices = [
        {
            "name": "TV",
            "device_type": "P110",
            "ip_addr": "192.168.0.224",
            "substract": "Nonexistent",
        }
    ]
    client, _ = make_client(devices, {"TV": {"power": 42}})
    body = client.get("/get_all_device_power").json()
    tv = body[0]

    assert tv["data"]["current_power"] == 42
    assert tv["data"]["subtraction_error"] == (
        "Device to subtract 'Nonexistent' not found"
    )


def test_unreachable_device_fails_without_breaking_the_response(make_client):
    client, _ = make_client(
        PLUGS,
        {
            "UPS": {"power": 76},
            "TV": {"error": "boom"},
            "Washer": {"power": 1},
        },
    )
    body = client.get("/get_all_device_power").json()
    tv = next(entry for entry in body if entry["device"] == "TV")

    assert tv["status"] == "failed"
    assert "data" not in tv
    # A failed device is left out of the total rather than counted as zero.
    assert body[-1]["data"]["included_devices"] == ["UPS", "Washer"]
    assert body[-1]["data"]["current_power"] == 77


def test_subtraction_skipped_when_the_target_failed(make_client):
    client, _ = make_client(
        PLUGS,
        {"UPS": {"error": "boom"}, "TV": {"power": 164}, "Washer": {"power": 1}},
    )
    body = client.get("/get_all_device_power").json()
    tv = next(entry for entry in body if entry["device"] == "TV")

    assert tv["data"]["current_power"] == 164
    assert tv["data"]["subtraction_error"] == "No valid power data for 'UPS'"


def test_entry_missing_name_is_skipped_in_place(make_client):
    devices = [
        {"name": "UPS", "device_type": "P115", "ip_addr": "192.168.0.103"},
        {"device_type": "P110", "ip_addr": "192.168.0.224"},
        {"name": "Washer", "device_type": "P115", "ip_addr": "192.168.0.45"},
    ]
    client, _ = make_client(devices, {"UPS": {"power": 5}, "Washer": {"power": 7}})
    body = client.get("/get_all_device_power").json()

    assert body[1]["status"] == "skipped"
    assert body[1]["error"] == "Missing 'name' or 'device_type' in devices.json entry"
    assert [entry.get("device") for entry in body] == [
        "UPS",
        None,
        "Washer",
        "Total Consumption",
    ]


def test_serialisation_matches_flask_jsonify(make_client):
    """Sorted keys, ASCII escaping and a trailing newline, as Flask produced."""
    client, _ = make_client([PLUGS[0]], {"UPS": {"power": 76}})
    response = client.get("/get_all_device_power")

    assert response.headers["content-type"].startswith("application/json")
    assert response.text.endswith("\n")

    parsed = json.loads(response.text)
    assert response.text == json.dumps(parsed, sort_keys=True, ensure_ascii=True) + "\n"


def test_initialization_error_reports_500(make_client):
    client, service = make_client(PLUGS)
    service.init_error = "Tapo credentials are missing"

    response = client.get("/get_all_device_power")
    assert response.status_code == 500
    assert response.json() == {
        "error": "Server initialization failed. Check logs.",
        "details": "Tapo credentials are missing",
    }


def test_root_redirects_to_the_aggregate(make_client):
    client, _ = make_client(PLUGS)
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/get_all_device_power"


def test_devices_are_polled_concurrently(make_client):
    """All three readings should be in flight together, not one after another."""
    import asyncio
    import time

    import tapo_power

    client, service = make_client(PLUGS)

    async def slow_raw(self, method, params=None):
        await asyncio.sleep(0.2)
        return {"current_power": 10}

    for device in service.registry.devices.values():
        device.raw = slow_raw.__get__(device)

    started = time.monotonic()
    client.get("/get_all_device_power")
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"sequential polling suspected ({elapsed:.2f}s for 3 devices)"
    assert tapo_power.DEVICE_TIMEOUT_SECONDS == 10
