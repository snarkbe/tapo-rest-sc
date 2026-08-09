"""Unit tests for the payload transforms that mirror what the `tapo` crate did."""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tapo_devices import (  # noqa: E402
    COLOR_PRESETS,
    DEVICE_GROUPS,
    GROUP_BY_MODEL,
    _add_months,
    action_uris,
    build_energy_data_request,
    normalise_payload,
    shape_energy_data,
)


def _local_epoch(*args) -> int:
    return int(datetime(*args).astimezone().timestamp())


def test_ssid_and_nickname_are_base64_decoded():
    payload = {"ssid": "R1JFODA1NA==", "nickname": "V2FzaGVy", "model": "P115"}
    assert normalise_payload(payload) == {
        "ssid": "GRE8054",
        "nickname": "Washer",
        "model": "P115",
    }


def test_undecodable_base64_is_left_alone():
    assert normalise_payload({"ssid": "not base64!"})["ssid"] == "not base64!"


def test_local_time_is_rendered_iso():
    payload = {"local_time": "2026-08-09 18:29:18"}
    assert normalise_payload(payload)["local_time"] == "2026-08-09T18:29:18"


def test_normalisation_recurses_into_nested_objects():
    payload = {"child_device_list": [{"nickname": "V2FzaGVy"}]}
    assert normalise_payload(payload)["child_device_list"][0]["nickname"] == "Washer"


def test_hourly_request_spans_midnight_to_end_of_day():
    request = build_energy_data_request(60, date(2026, 8, 9), date(2026, 8, 10))
    assert request == {
        "start_timestamp": _local_epoch(2026, 8, 9, 0, 0, 0),
        "end_timestamp": _local_epoch(2026, 8, 10, 23, 59, 59),
        "interval": 60,
    }


def test_hourly_request_defaults_end_date_to_start_date():
    request = build_energy_data_request(60, date(2026, 8, 9))
    assert request["end_timestamp"] == _local_epoch(2026, 8, 9, 23, 59, 59)


def test_daily_and_monthly_requests_use_one_timestamp():
    daily = build_energy_data_request(1440, date(2026, 7, 1))
    monthly = build_energy_data_request(43200, date(2026, 1, 1))

    assert daily["start_timestamp"] == daily["end_timestamp"] == _local_epoch(2026, 7, 1)
    assert daily["interval"] == 1440
    assert monthly["start_timestamp"] == monthly["end_timestamp"] == _local_epoch(2026, 1, 1)
    assert monthly["interval"] == 43200


def test_hourly_entries_advance_by_one_hour():
    start = _local_epoch(2026, 8, 9, 0, 0, 0)
    shaped = shape_energy_data(
        {
            "local_time": "2026-08-09 18:30:50",
            "data": [72, 87, 92],
            "start_timestamp": start,
            "interval": 60,
        }
    )

    assert shaped["interval_length"] == 60
    assert shaped["local_time"] == "2026-08-09T18:30:50"
    assert [entry["energy"] for entry in shaped["entries"]] == [72, 87, 92]

    stamps = [entry["start_date_time"] for entry in shaped["entries"]]
    parsed = [datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ") for s in stamps]
    assert (parsed[1] - parsed[0]).total_seconds() == 3600
    assert (parsed[2] - parsed[1]).total_seconds() == 3600
    assert shaped["start_date_time"] == stamps[0]


def test_start_date_time_is_utc_with_a_z_suffix():
    start = int(datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    shaped = shape_energy_data(
        {"local_time": "2026-08-09 14:00:00", "data": [1], "start_timestamp": start, "interval": 60}
    )
    assert shaped["start_date_time"] == "2026-08-09T12:00:00Z"


def test_monthly_entries_advance_by_calendar_months():
    start = _local_epoch(2026, 1, 1)
    shaped = shape_energy_data(
        {
            "local_time": "2026-08-09 18:30:50",
            "data": [1, 2, 3],
            "start_timestamp": start,
            "interval": 43200,
        }
    )
    # Stamps are emitted in UTC; convert back to local to read the calendar month.
    months = [
        datetime.strptime(entry["start_date_time"], "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .astimezone()
        for entry in shaped["entries"]
    ]
    # January, February, March -- not fixed 30-day steps.
    assert [m.month for m in months] == [1, 2, 3]
    assert all(m.day == 1 for m in months)


def test_add_months_clamps_the_day():
    assert _add_months(datetime(2026, 1, 31), 1) == datetime(2026, 2, 28)
    assert _add_months(datetime(2026, 12, 15), 1) == datetime(2027, 1, 15)


def test_every_model_belongs_to_exactly_one_group():
    models = [model for group in DEVICE_GROUPS for model in group.models]
    assert len(models) == len(set(models)) == 18
    assert set(GROUP_BY_MODEL) == set(models)


def test_action_uris_cover_the_documented_surface():
    uris = action_uris()
    assert len(uris) == len(set(uris))

    energy_plug = [uri for uri in uris if uri.startswith("/p115/")]
    assert sorted(energy_plug) == sorted(
        [
            "/p115/on",
            "/p115/off",
            "/p115/get-device-info",
            "/p115/get-device-usage",
            "/p115/get-energy-usage",
            "/p115/get-hourly-energy-data",
            "/p115/get-daily-energy-data",
            "/p115/get-monthly-energy-data",
            "/p115/get-current-power",
        ]
    )
    assert sorted(uri for uri in uris if uri.startswith("/p300/")) == [
        "/p300/get-child-device-list",
        "/p300/get-device-info",
    ]


def test_colour_presets_match_the_tapo_crate():
    assert len(COLOR_PRESETS) == 41
    assert COLOR_PRESETS["HotPink"] == (330, 58, 0)
    assert COLOR_PRESETS["Incandescent"] == (0, 100, 2700)
