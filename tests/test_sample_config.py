"""The committed sample must actually work when copied to app/config.json.

An earlier sample carried a placeholder API key containing underscores, which
made the server refuse to start for anyone who copied it verbatim.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tapo_config import KNOWN_DEVICE_TYPES, load_config  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "app" / "config.sample.json"


def test_sample_config_exists_and_is_tracked_by_name():
    assert SAMPLE.is_file(), "app/config.sample.json is the committed template"


def test_sample_config_loads_without_error():
    config = load_config(SAMPLE)

    assert config.email and config.password
    assert len(config.devices) == 3
    assert all(d.device_type in KNOWN_DEVICE_TYPES for d in config.devices)


def test_sample_config_needs_no_api_key_to_start():
    """API keys are optional: only the /actions routes require one."""
    config = load_config(SAMPLE)

    assert config.api_keys == ()
    assert config.has_api_keys() is False


def test_sample_config_demonstrates_chaining():
    config = load_config(SAMPLE)
    chained = [d for d in config.devices if d.substract]

    assert len(chained) == 1
    # The device it points at must exist, or the example teaches a broken config.
    assert chained[0].substract in {d.name for d in config.devices}


def test_sample_config_carries_no_real_secrets():
    text = SAMPLE.read_text(encoding="utf-8")

    assert "example.com" in text
    assert "192.168.1." in text, "use documentation-style addresses, not a real LAN"
