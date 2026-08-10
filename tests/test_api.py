"""The device REST API: routing, auth, validation and the JSON error contract."""

from __future__ import annotations

from conftest import VALID_API_KEY, FakeLight, FakeLightEffect

PLUGS = [
    {"name": "Washer", "device_type": "P115", "ip_addr": "192.168.0.45"},
    {"name": "Bulb", "device_type": "L530", "ip_addr": "192.168.0.50"},
]

AUTH = {"Authorization": f"Bearer {VALID_API_KEY}"}


def _lit_bulb(effects=("Aurora", "Rainbow")):
    """Fake-device kwargs for a bulb carrying both light modules."""
    return {
        "power": 0,
        "modules": {"Light": FakeLight(), "LightEffect": FakeLightEffect(effects)},
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_missing_authorization_header_is_a_401(make_client):
    client, _ = make_client(PLUGS)
    response = client.get("/devices")

    assert response.status_code == 401
    assert response.json()["detail"].startswith("Missing or malformed")
    assert response.headers["www-authenticate"] == "Bearer"


def test_non_bearer_scheme_is_a_401(make_client):
    client, _ = make_client(PLUGS)
    response = client.get("/devices", headers={"Authorization": "Basic " + "a" * 32})

    assert response.status_code == 401


def test_wrong_api_key_is_a_403(make_client):
    client, _ = make_client(PLUGS)
    response = client.get("/devices", headers={"Authorization": "Bearer " + "b" * 32})

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid API key"}


def test_no_configured_keys_means_everything_is_rejected(make_client):
    client, _ = make_client(PLUGS, api_keys=())

    assert client.get("/devices", headers=AUTH).status_code == 403
    # ... while the aggregated reading keeps working without a key.
    assert client.get("/get_all_device_power").status_code == 200


# ---------------------------------------------------------------------------
# Device lookup
# ---------------------------------------------------------------------------


def test_devices_lists_connection_infos(make_client):
    client, _ = make_client(PLUGS)
    response = client.get("/devices", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == [
        {"name": "Washer", "device_type": "P115", "ip_addr": "192.168.0.45"},
        {"name": "Bulb", "device_type": "L530", "ip_addr": "192.168.0.50"},
    ]


def test_unknown_device_is_a_404_naming_it(make_client):
    client, _ = make_client(PLUGS)
    response = client.get("/devices/Nope/power", headers=AUTH)

    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown device: Nope"}


def test_device_names_with_spaces_work_as_path_segments(make_client):
    client, _ = make_client(
        [{"name": "Living room plug", "device_type": "P110", "ip_addr": "192.168.0.7"}],
        {"Living room plug": {"power": 12}},
    )
    response = client.get("/devices/Living room plug/power", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"current_power": 12}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_current_power_returns_the_device_payload(make_client):
    client, _ = make_client(PLUGS, {"Washer": {"power": 813}})
    response = client.get("/devices/Washer/power", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"current_power": 813}


def test_device_info_is_served_from_the_device(make_client):
    client, _ = make_client(
        PLUGS, {"Washer": {"payloads": {"get_device_info": {"model": "P115"}}}}
    )
    response = client.get("/devices/Washer", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"model": "P115"}


def test_energy_history_sends_the_interval_the_device_expects(make_client):
    client, service = make_client(
        PLUGS, {"Washer": {"payloads": {"get_energy_data": {"data": [1, 2]}}}}
    )
    response = client.get(
        "/devices/Washer/energy/history",
        params={"interval": "monthly", "start_date": "2026-01-01"},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json() == {"data": [1, 2]}
    method, params = service.registry.get("Washer").calls[-1]
    assert method == "get_energy_data"
    assert params["interval"] == 43200


def test_energy_history_defaults_to_daily(make_client):
    client, service = make_client(
        PLUGS, {"Washer": {"payloads": {"get_energy_data": {"data": []}}}}
    )
    response = client.get(
        "/devices/Washer/energy/history",
        params={"start_date": "2026-08-09"},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert service.registry.get("Washer").calls[-1][1]["interval"] == 1440


def test_children_pages_through_the_device_list(make_client):
    strip = [{"name": "Strip", "device_type": "P300", "ip_addr": "192.168.0.60"}]
    client, _ = make_client(
        strip,
        {
            "Strip": {
                "payloads": {
                    "get_child_device_list": {
                        "child_device_list": [{"nickname": "V2FzaGVy"}],
                        "sum": 1,
                    }
                }
            }
        },
    )
    response = client.get("/devices/Strip/children", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == [{"nickname": "Washer"}]


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------


def test_turning_on_sends_the_right_command(make_client):
    client, service = make_client(PLUGS)
    response = client.post("/devices/Washer/on", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"name": "Washer", "on": True}
    assert service.registry.get("Washer").calls == [
        ("set_device_info", {"device_on": True})
    ]


def test_turning_off_sends_the_right_command(make_client):
    client, service = make_client(PLUGS)
    response = client.post("/devices/Washer/off", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"name": "Washer", "on": False}
    assert service.registry.get("Washer").calls == [
        ("set_device_info", {"device_on": False})
    ]


def test_get_on_a_control_route_is_a_405(make_client):
    client, _ = make_client(PLUGS)
    assert client.get("/devices/Washer/on", headers=AUTH).status_code == 405
    assert client.get("/reload-config", headers=AUTH).status_code == 405


def test_setting_several_light_values_at_once(make_client):
    client, service = make_client(PLUGS, {"Bulb": _lit_bulb()})
    response = client.post(
        "/devices/Bulb/light",
        params={"brightness": 40, "hue": 200, "saturation": 60},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json() == {
        "name": "Bulb",
        "applied": {"brightness": 40, "hue": 200, "saturation": 60},
    }
    light = service.registry.get("Bulb").modules["Light"]
    assert light.calls == [("set_brightness", 40), ("set_hsv", 200, 60)]


def test_effect_names_are_matched_case_insensitively(make_client):
    client, service = make_client(PLUGS, {"Bulb": _lit_bulb()})
    response = client.post(
        "/devices/Bulb/light", params={"effect": "rainbow"}, headers=AUTH
    )

    assert response.status_code == 200
    # The strip's own spelling comes back, not what the caller typed.
    assert response.json()["applied"] == {"effect": "Rainbow"}
    assert service.registry.get("Bulb").modules["LightEffect"].effect == "Rainbow"


def test_an_effect_the_device_does_not_offer_is_a_400(make_client):
    client, _ = make_client(PLUGS, {"Bulb": _lit_bulb()})
    response = client.post(
        "/devices/Bulb/light", params={"effect": "Octarine"}, headers=AUTH
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "does not offer the 'Octarine' effect" in detail
    assert "Aurora, Rainbow" in detail


def test_a_light_command_on_a_plug_is_a_400_naming_the_feature(make_client):
    """No table of models: the device reports it has no Light module."""
    client, _ = make_client(PLUGS)
    response = client.post(
        "/devices/Washer/light", params={"brightness": 50}, headers=AUTH
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Device 'Washer' (P115) does not support the 'Light' feature"
    }


def test_a_light_command_with_no_parameters_is_a_400(make_client):
    client, _ = make_client(PLUGS, {"Bulb": _lit_bulb()})
    response = client.post("/devices/Bulb/light", headers=AUTH)

    assert response.status_code == 400
    assert "at least one of" in response.json()["detail"]


def test_hue_without_saturation_is_a_400(make_client):
    client, _ = make_client(PLUGS, {"Bulb": _lit_bulb()})
    response = client.post("/devices/Bulb/light", params={"hue": 200}, headers=AUTH)

    assert response.status_code == 400
    assert "must be given together" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Validation and failures
# ---------------------------------------------------------------------------


def test_out_of_range_brightness_is_a_422(make_client):
    client, _ = make_client(PLUGS, {"Bulb": _lit_bulb()})
    response = client.post(
        "/devices/Bulb/light", params={"brightness": 300}, headers=AUTH
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "brightness"]


def test_a_malformed_date_is_a_422(make_client):
    client, _ = make_client(PLUGS)
    response = client.get(
        "/devices/Washer/energy/history",
        params={"start_date": "31-12-2026"},
        headers=AUTH,
    )

    assert response.status_code == 422


def test_a_missing_start_date_is_a_422(make_client):
    client, _ = make_client(PLUGS)
    response = client.get("/devices/Washer/energy/history", headers=AUTH)

    assert response.status_code == 422


def test_an_unknown_interval_is_a_422(make_client):
    client, _ = make_client(PLUGS)
    response = client.get(
        "/devices/Washer/energy/history",
        params={"interval": "yearly", "start_date": "2026-08-09"},
        headers=AUTH,
    )

    assert response.status_code == 422


def test_device_failure_surfaces_as_a_502_in_json(make_client):
    client, _ = make_client(PLUGS, {"Washer": {"error": "connection refused"}})
    response = client.get("/devices/Washer/power", headers=AUTH)

    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/json")
    assert "connection refused" in response.json()["detail"]


# ---------------------------------------------------------------------------
# The routes tapo-rest used to serve are gone
# ---------------------------------------------------------------------------


def test_the_old_action_routes_are_gone(make_client):
    client, _ = make_client(PLUGS)

    for path in ("/actions", "/actions/p115/get-current-power", "/refresh-session"):
        assert client.get(path, params={"device": "Washer"}, headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# The API documents itself
# ---------------------------------------------------------------------------


def test_openapi_documents_the_device_routes(make_client):
    client, _ = make_client(PLUGS)
    schema = client.get("/openapi.json").json()

    assert "/devices/{name}/power" in schema["paths"]
    assert "/devices/{name}/light" in schema["paths"]
    # The frozen aggregated endpoint stays out of the schema.
    assert "/get_all_device_power" not in schema["paths"]


def test_openapi_advertises_the_bearer_scheme(make_client):
    client, _ = make_client(PLUGS)
    schema = client.get("/openapi.json").json()

    schemes = schema["components"]["securitySchemes"]
    assert any(s.get("scheme") == "bearer" for s in schemes.values())
    assert schema["paths"]["/devices"]["get"]["security"]
