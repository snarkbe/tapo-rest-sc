"""Unit tests for payload normalisation and energy request building."""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tapo_devices import (  # noqa: E402
    ENERGY_INTERVALS,
    energy_data_request,
    normalise_payload,
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
    request = energy_data_request(60, date(2026, 8, 9), date(2026, 8, 10))
    assert request == {
        "start_timestamp": _local_epoch(2026, 8, 9, 0, 0, 0),
        "end_timestamp": _local_epoch(2026, 8, 10, 23, 59, 59),
        "interval": 60,
    }


def test_hourly_request_defaults_end_date_to_start_date():
    request = energy_data_request(60, date(2026, 8, 9))
    assert request["end_timestamp"] == _local_epoch(2026, 8, 9, 23, 59, 59)


def test_daily_and_monthly_requests_use_one_timestamp():
    daily = energy_data_request(1440, date(2026, 7, 1))
    monthly = energy_data_request(43200, date(2026, 1, 1))

    assert daily["start_timestamp"] == daily["end_timestamp"] == _local_epoch(2026, 7, 1)
    assert daily["interval"] == 1440
    assert monthly["start_timestamp"] == monthly["end_timestamp"] == _local_epoch(2026, 1, 1)
    assert monthly["interval"] == 43200


def test_the_three_intervals_are_the_ones_the_devices_accept():
    assert ENERGY_INTERVALS == {"hourly": 60, "daily": 1440, "monthly": 43200}
