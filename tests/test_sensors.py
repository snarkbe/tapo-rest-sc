"""The /get_all_sensor_data contract, read by a Homepage widget like the power one."""

from __future__ import annotations

import base64
import json

from tapo_devices import normalise_payload

PLUG = {"name": "Washer", "device_type": "P115", "ip_addr": "192.168.0.45"}
HUB = {"name": "Smart Hub", "device_type": "H100", "ip_addr": "192.168.0.153"}


def _nickname(name: str) -> str:
    return base64.b64encode(name.encode()).decode()


# Trimmed from what an H100 answers to `get_child_device_list`.
T310 = {
    "model": "T310",
    "nickname": _nickname("Tapo T310"),
    "device_id": "child-1",
    "status": "online",
    "at_low_battery": False,
    "current_temp": 20.8,
    "current_temp_exception": 0,
    "current_humidity": 41,
    "current_humidity_exception": 0,
    "temp_unit": "celsius",
    "type": "SMART.TAPOSENSOR",
}
T315 = {
    **T310,
    "model": "T315",
    "nickname": _nickname("Tapo T315 Bedroom"),
    "device_id": "child-2",
    "current_temp": 17.0,
    # Below the comfort range the app is set to: shown in red there.
    "current_temp_exception": -2.0,
    "current_humidity": 48,
}
T300 = {
    "model": "T300",
    "nickname": _nickname("Water Leak Sensor"),
    "device_id": "child-3",
    "status": "online",
    "at_low_battery": True,
    "water_leak_status": "normal",
    "in_alarm": False,
}


def hub_payload(*children):
    return {
        "get_child_device_list": {
            "child_device_list": list(children),
            "start_index": 0,
            "sum": len(children),
        }
    }


def test_every_sensor_is_listed_with_its_hub(make_client):
    client, _ = make_client(
        [PLUG],
        hubs=[HUB],
        powers={"Smart Hub": {"payloads": hub_payload(T310, T315, T300)}},
    )
    body = client.get("/get_all_sensor_data").json()

    assert [entry["device"] for entry in body] == [
        "Tapo T310",
        "Tapo T315 Bedroom",
        "Water Leak Sensor",
    ]
    assert {entry["hub"] for entry in body} == {"Smart Hub"}
    assert all(entry["status"] == "success" for entry in body)


def test_climate_sensor_shape(make_client):
    client, _ = make_client(
        [PLUG], hubs=[HUB], powers={"Smart Hub": {"payloads": hub_payload(T310)}}
    )
    (entry,) = client.get("/get_all_sensor_data").json()

    assert entry == {
        "device": "Tapo T310",
        "hub": "Smart Hub",
        "model": "T310",
        "status": "success",
        "data": {
            "temperature": 20.8,
            "temperature_unit": "celsius",
            "temperature_warning": False,
            "humidity": 41,
            "humidity_warning": False,
            "low_battery": False,
            "online": True,
            "summary": "20.8°C | 41%",
        },
    }


def test_out_of_comfort_range_is_flagged(make_client):
    client, _ = make_client(
        [PLUG], hubs=[HUB], powers={"Smart Hub": {"payloads": hub_payload(T315)}}
    )
    (entry,) = client.get("/get_all_sensor_data").json()

    assert entry["data"]["temperature_warning"] is True
    assert entry["data"]["humidity_warning"] is False


def test_leak_sensor_carries_no_temperature(make_client):
    client, _ = make_client(
        [PLUG], hubs=[HUB], powers={"Smart Hub": {"payloads": hub_payload(T300)}}
    )
    (entry,) = client.get("/get_all_sensor_data").json()

    assert "temperature" not in entry["data"]
    assert entry["data"]["water_leak"] is False
    assert entry["data"]["low_battery"] is True
    assert entry["data"]["summary"] == "Dry"


def test_a_leak_is_reported(make_client):
    leaking = {**T300, "water_leak_status": "water_leak", "in_alarm": True}
    client, _ = make_client(
        [PLUG], hubs=[HUB], powers={"Smart Hub": {"payloads": hub_payload(leaking)}}
    )
    (entry,) = client.get("/get_all_sensor_data").json()

    assert entry["data"]["water_leak"] is True
    assert entry["data"]["summary"] == "Leak!"


def test_fahrenheit_is_labelled(make_client):
    sensor = {**T310, "temp_unit": "fahrenheit", "current_temp": 69.4}
    client, _ = make_client(
        [PLUG], hubs=[HUB], powers={"Smart Hub": {"payloads": hub_payload(sensor)}}
    )
    (entry,) = client.get("/get_all_sensor_data").json()

    assert entry["data"]["summary"] == "69.4°F | 41%"


def test_an_offline_sensor_keeps_its_last_reading_but_says_so(make_client):
    sensor = {**T310, "status": "offline"}
    client, _ = make_client(
        [PLUG], hubs=[HUB], powers={"Smart Hub": {"payloads": hub_payload(sensor)}}
    )
    (entry,) = client.get("/get_all_sensor_data").json()

    assert entry["data"]["online"] is False
    assert entry["data"]["temperature"] == 20.8


def test_an_unreachable_hub_is_one_failed_entry(make_client):
    client, _ = make_client(
        [PLUG], hubs=[HUB], powers={"Smart Hub": {"error": "connection refused"}}
    )
    response = client.get("/get_all_sensor_data")

    assert response.status_code == 200
    (entry,) = response.json()
    assert entry["device"] == "Smart Hub"
    assert entry["status"] == "failed"
    assert "connection refused" in entry["details"]


def test_children_are_paged(make_client):
    pages = {
        0: {"child_device_list": [T310], "start_index": 0, "sum": 2},
        1: {"child_device_list": [T315], "start_index": 1, "sum": 2},
    }
    client, service = make_client([PLUG], hubs=[HUB])
    hub = service.registry.get("Smart Hub")

    async def raw(method, params=None):
        hub.calls.append((method, params))
        return normalise_payload(pages[params["start_index"]])

    hub.raw = raw
    body = client.get("/get_all_sensor_data").json()

    assert [entry["device"] for entry in body] == ["Tapo T310", "Tapo T315 Bedroom"]
    assert [params for _, params in hub.calls] == [{"start_index": 0}, {"start_index": 1}]


def test_no_hubs_means_an_empty_array(make_client):
    client, _ = make_client([PLUG])

    assert client.get("/get_all_sensor_data").json() == []


def test_needs_no_api_key(make_client):
    client, _ = make_client([PLUG], hubs=[HUB], api_keys=())

    assert client.get("/get_all_sensor_data").status_code == 200


def test_serialisation_matches_the_power_endpoint(make_client):
    client, _ = make_client(
        [PLUG], hubs=[HUB], powers={"Smart Hub": {"payloads": hub_payload(T310)}}
    )
    response = client.get("/get_all_sensor_data")

    assert response.text.endswith("\n")
    assert "\\u00b0C" in response.text  # ASCII-escaped, as Flask's jsonify did
    assert json.loads(response.text)[0]["data"]["summary"] == "20.8°C | 41%"


def test_a_hub_is_left_out_of_the_power_total(make_client):
    client, service = make_client(
        [PLUG], hubs=[HUB], powers={"Washer": {"power": 12}}
    )
    body = client.get("/get_all_device_power").json()

    assert [entry["device"] for entry in body] == ["Washer", "Total Consumption"]
    assert service.registry.get("Smart Hub").calls == []


def test_a_hub_is_reachable_on_the_device_routes(make_client):
    from conftest import VALID_API_KEY

    client, _ = make_client(
        [PLUG], hubs=[HUB], powers={"Smart Hub": {"payloads": hub_payload(T310)}}
    )
    response = client.get(
        "/devices/smart-hub/children",
        headers={"Authorization": f"Bearer {VALID_API_KEY}"},
    )

    assert response.status_code == 200
    assert response.json()[0]["nickname"] == "Tapo T310"
