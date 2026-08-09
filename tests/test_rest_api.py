"""The tapo-rest compatible surface: routing, auth and the plain-text error contract."""

from __future__ import annotations

from conftest import VALID_API_KEY

PLUGS = [
    {"name": "Washer", "device_type": "P115", "ip_addr": "192.168.0.45"},
    {"name": "Bulb", "device_type": "L530", "ip_addr": "192.168.0.50"},
]

AUTH = {"Authorization": f"Bearer {VALID_API_KEY}"}


def test_actions_listing_needs_no_auth(make_client):
    client, _ = make_client(PLUGS)
    response = client.get("/actions")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")

    lines = response.text.split("\n")
    assert "/p115/get-current-power" in lines
    assert "/l530/set-hue-saturation" in lines
    assert "/p300/get-child-device-list" in lines
    # Every model tapo-rest knew about is present.
    assert len({line.split("/")[1] for line in lines}) == 18


def test_missing_authorization_header_is_a_400(make_client):
    client, _ = make_client(PLUGS)
    response = client.get("/devices")

    assert response.status_code == 400
    assert response.text == "Header of type `authorization` was missing"


def test_wrong_api_key_is_a_403(make_client):
    client, _ = make_client(PLUGS)
    response = client.get("/devices", headers={"Authorization": "Bearer " + "b" * 32})

    assert response.status_code == 403
    assert response.text == "Invalid bearer token"


def test_no_configured_keys_means_everything_is_rejected(make_client):
    client, _ = make_client(PLUGS, api_keys=())
    response = client.get("/devices", headers=AUTH)

    assert response.status_code == 403


def test_devices_lists_connection_infos(make_client):
    client, _ = make_client(PLUGS)
    response = client.get("/devices", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == [
        {"name": "Washer", "device_type": "P115", "ip_addr": "192.168.0.45"},
        {"name": "Bulb", "device_type": "L530", "ip_addr": "192.168.0.50"},
    ]


def test_get_current_power_returns_the_raw_device_shape(make_client):
    client, _ = make_client(PLUGS, {"Washer": {"power": 813}})
    response = client.get(
        "/actions/p115/get-current-power", params={"device": "Washer"}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.json() == {"current_power": 813}


def test_unknown_device_is_a_404(make_client):
    client, _ = make_client(PLUGS)
    response = client.get(
        "/actions/p115/get-current-power", params={"device": "Nope"}, headers=AUTH
    )

    assert response.status_code == 404
    assert response.text == "Provided device name was not found"


def test_missing_device_parameter_is_a_400(make_client):
    client, _ = make_client(PLUGS)
    response = client.get("/actions/p115/get-current-power", headers=AUTH)

    assert response.status_code == 400
    assert "missing field `device`" in response.text


def test_wrong_model_group_is_a_400_naming_both_types(make_client):
    client, _ = make_client(PLUGS)
    response = client.get(
        "/actions/l510/on", params={"device": "Washer"}, headers=AUTH
    )

    assert response.status_code == 400
    assert response.text == (
        "This route is reserved to L510, L520, L610 devices, "
        "but the provided name refers to a P115 device"
    )


def test_aliased_models_share_a_route(make_client):
    """A P115 answers on the p110 route: they are one group, as in tapo-rest."""
    client, _ = make_client(PLUGS, {"Washer": {"power": 5}})
    response = client.get(
        "/actions/p110/get-current-power", params={"device": "Washer"}, headers=AUTH
    )

    assert response.status_code == 200


def test_energy_route_is_absent_for_non_energy_plugs(make_client):
    client, _ = make_client(PLUGS)
    response = client.get(
        "/actions/p100/get-current-power", params={"device": "Washer"}, headers=AUTH
    )

    assert response.status_code == 404


def test_missing_required_action_parameter_is_a_400(make_client):
    client, _ = make_client(PLUGS)
    response = client.get(
        "/actions/p115/get-daily-energy-data", params={"device": "Washer"}, headers=AUTH
    )

    assert response.status_code == 400
    assert "missing field `start_date`" in response.text


def test_malformed_date_is_a_400(make_client):
    client, _ = make_client(PLUGS)
    response = client.get(
        "/actions/p115/get-daily-energy-data",
        params={"device": "Washer", "start_date": "31-12-2026"},
        headers=AUTH,
    )

    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.text


def test_out_of_range_brightness_is_a_400(make_client):
    client, _ = make_client(PLUGS)
    response = client.get(
        "/actions/l530/set-brightness",
        params={"device": "Bulb", "level": "300"},
        headers=AUTH,
    )

    assert response.status_code == 400
    assert "out of range" in response.text


def test_unknown_colour_preset_is_a_400(make_client):
    client, _ = make_client(PLUGS)
    response = client.get(
        "/actions/l530/set-color",
        params={"device": "Bulb", "color": "Octarine"},
        headers=AUTH,
    )

    assert response.status_code == 400
    assert "unknown colour" in response.text


def test_turning_on_returns_an_empty_200(make_client):
    """Exercised against a fake device only -- never against the real plugs."""
    client, service = make_client(PLUGS)
    response = client.get("/actions/p115/on", params={"device": "Washer"}, headers=AUTH)

    assert response.status_code == 200
    assert response.content == b""
    assert service.registry.get("Washer").calls == [
        ("set_device_info", {"device_on": True})
    ]


def test_turning_off_sends_the_right_command(make_client):
    client, service = make_client(PLUGS)
    response = client.get("/actions/p115/off", params={"device": "Washer"}, headers=AUTH)

    assert response.status_code == 200
    assert service.registry.get("Washer").calls == [
        ("set_device_info", {"device_on": False})
    ]


def test_device_failure_surfaces_as_a_500(make_client):
    client, _ = make_client(PLUGS, {"Washer": {"error": "connection refused"}})
    response = client.get(
        "/actions/p115/get-current-power", params={"device": "Washer"}, headers=AUTH
    )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/plain")
    assert "connection refused" in response.text


def test_refresh_session_reconnects(make_client):
    client, service = make_client(PLUGS)
    response = client.get(
        "/refresh-session", params={"device": "Washer"}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.content == b""
    assert service.registry.get("Washer").refreshed == 1


def test_refresh_session_unknown_device_uses_its_own_message(make_client):
    client, _ = make_client(PLUGS)
    response = client.get("/refresh-session", params={"device": "Nope"}, headers=AUTH)

    assert response.status_code == 404
    assert response.text == "Unknown device: Nope"


def test_reload_config_rejects_get(make_client):
    client, _ = make_client(PLUGS)
    assert client.get("/reload-config", headers=AUTH).status_code == 405
