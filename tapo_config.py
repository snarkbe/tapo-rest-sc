"""Configuration loading for taposc.

A single JSON file holds everything: the Tapo account credentials, the server's
API keys and the device list. It is a superset of the format earlier versions
of this project read, so an existing `devices.json` loads unchanged.

    {
        "tapo_credentials": { "email": "...", "password": "..." },
        "server": { "api_keys": [ { "name": "...", "key": "<32+ alnum>" } ] },
        "devices": [
            { "name": "Washer", "device_type": "P115", "ip_addr": "192.168.0.45" },
            { "name": "TV", "device_type": "P110", "ip_addr": "192.168.0.224",
              "substract": "Washer" }
        ]
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Device types this project was written against. Nothing dispatches on them --
# they only label log and error messages -- so an unfamiliar one is a warning,
# not a rejection, and a newer model needs no code change.
KNOWN_DEVICE_TYPES = (
    "L510", "L520", "L530", "L535", "L610", "L630",
    "L900", "L920", "L930",
    "P100", "P105", "P110", "P110M", "P115",
    "P300", "P304", "P304M", "P316",
)  # fmt: skip

MIN_API_KEY_LENGTH = 32

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """A URL-safe stand-in for a device name.

    Device names are descriptive -- "UPS: NAS / Router / Fiber" -- and a `/` in
    one cannot survive a URL path, since percent-encoding is undone before
    routing. The slug ("ups-nas-router-fiber") gives such a device an address.
    Returns an empty string for a name with nothing ASCII in it.
    """
    ascii_only = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    return _NON_SLUG.sub("-", ascii_only.lower()).strip("-")


class ConfigError(Exception):
    """Raised when the configuration file is missing, unparseable or invalid."""


@dataclass(frozen=True)
class ApiKey:
    name: str
    key: str


@dataclass(frozen=True)
class DeviceEntry:
    name: str
    device_type: str
    ip_addr: str
    # taposc-only: name of another device whose power is subtracted from this one.
    substract: str | None = None

    @property
    def slug(self) -> str:
        """The URL-safe identifier this device also answers to."""
        return slugify(self.name)

    def conn_infos(self) -> dict:
        """The shape `/devices` returns."""
        return {
            "name": self.name,
            "slug": self.slug,
            "device_type": self.device_type,
            "ip_addr": self.ip_addr,
        }


@dataclass(frozen=True)
class InvalidDeviceEntry:
    """A device entry we could not use. Reported as "skipped", keeping its position."""

    raw: dict
    reason: str


@dataclass(frozen=True)
class Config:
    path: Path
    email: str
    password: str
    # Valid devices only, in config order -- what the registry connects to.
    devices: tuple[DeviceEntry, ...] = ()
    api_keys: tuple[ApiKey, ...] = ()
    # Every entry in config order, valid or not, so the aggregated power
    # response keeps reporting unusable entries in place.
    items: tuple[DeviceEntry | InvalidDeviceEntry, ...] = field(default=())

    def has_api_keys(self) -> bool:
        return bool(self.api_keys)

    def is_valid_api_key(self, candidate: str) -> bool:
        return any(entry.key == candidate for entry in self.api_keys)


def find_config_path() -> Path:
    """Resolve the config file: $TAPOSC_CONFIG, then app/config.json, then app/devices.json."""
    explicit = os.environ.get("TAPOSC_CONFIG")
    if explicit:
        return Path(explicit)

    base = Path(__file__).resolve().parent / "app"
    preferred = base / "config.json"
    if preferred.is_file():
        return preferred

    legacy = base / "devices.json"
    if legacy.is_file():
        logger.warning(
            "Reading configuration from the legacy filename %s. "
            "Rename it to config.json; support for the old name will go away.",
            legacy,
        )
        return legacy

    # Nothing exists yet: report the preferred path in the "not found" error.
    return preferred


def _read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            # Historic quirk: tolerate a single leading `//` comment line.
            first_line = handle.readline()
            if not first_line.strip().startswith("//"):
                handle.seek(0)
            return json.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"Configuration file not found at {path}") from None
    except json.JSONDecodeError as err:
        raise ConfigError(f"Could not decode JSON from {path}: {err}") from None
    except OSError as err:
        raise ConfigError(f"Could not read {path}: {err}") from None


def _reject_legacy_config(raw: dict, path: Path) -> None:
    """Catch the pre-python config, whose two keys no longer mean anything.

    It used to point the Flask app at a separate tapo-rest process. Devices are
    now contacted directly, so a file carrying only those keys is a leftover and
    deserves a better message than "credentials are missing".
    """
    if "devices" in raw or "tapo_credentials" in raw:
        return
    if "tapo_api_url" in raw or "login_password" in raw:
        raise ConfigError(
            f"{path} is the old configuration format: 'tapo_api_url' and "
            "'login_password' pointed at a separate tapo-rest process, which no "
            "longer exists. Replace this file with one based on "
            "app/config.sample.json (it needs 'tapo_credentials' and 'devices'), "
            "or delete it and rename your old devices.json to config.json."
        )


def _parse_credentials(raw: dict) -> tuple[str, str]:
    """Credentials from the file, with TAPO_EMAIL / TAPO_PASSWORD taking precedence."""
    credentials = raw.get("tapo_credentials") or {}
    if not isinstance(credentials, dict):
        raise ConfigError("'tapo_credentials' must be an object")

    email = os.environ.get("TAPO_EMAIL") or credentials.get("email")
    password = os.environ.get("TAPO_PASSWORD") or credentials.get("password")

    if not email or not password:
        raise ConfigError(
            "Tapo credentials are missing: set 'tapo_credentials' "
            "(email and password) in the configuration file, or the "
            "TAPO_EMAIL and TAPO_PASSWORD environment variables."
        )
    return email, password


def _parse_api_keys(raw: dict) -> tuple[ApiKey, ...]:
    """API keys from `server.api_keys`, or from the TAPO_API_KEYS env var.

    Older configurations carried a `server_password`, flat or inside `server`.
    It is not read by anything: only `server.api_keys` grants access now.
    """
    from_env = os.environ.get("TAPO_API_KEYS", "")
    entries: list[ApiKey] = [
        ApiKey(name=f"env#{index + 1}", key=key.strip())
        for index, key in enumerate(from_env.split(","))
        if key.strip()
    ]

    server = raw.get("server") or {}
    if not isinstance(server, dict):
        raise ConfigError("'server' must be an object")

    for entry in server.get("api_keys") or []:
        if not isinstance(entry, dict) or not entry.get("key"):
            raise ConfigError("Every entry of 'server.api_keys' needs a 'key'")
        entries.append(ApiKey(name=entry.get("name") or "unnamed", key=entry["key"]))

    for entry in entries:
        if not entry.key.isascii() or not entry.key.isalnum():
            raise ConfigError(
                f"API key '{entry.name}' contains non-alphanumeric characters"
            )
        if len(entry.key) < MIN_API_KEY_LENGTH:
            raise ConfigError(
                f"API key '{entry.name}' is too short "
                f"(minimum length: {MIN_API_KEY_LENGTH} characters)"
            )

    return tuple(entries)


def _parse_devices(
    raw: dict,
) -> tuple[tuple[DeviceEntry, ...], tuple[DeviceEntry | InvalidDeviceEntry, ...]]:
    devices = raw.get("devices")
    if devices is None:
        raise ConfigError("No 'devices' key in the configuration file")
    if not isinstance(devices, list):
        raise ConfigError("'devices' must be an array")

    parsed: list[DeviceEntry] = []
    items: list[DeviceEntry | InvalidDeviceEntry] = []
    seen: set[str] = set()

    for entry in devices:
        if not isinstance(entry, dict):
            logger.warning("Skipping device entry, not an object: %s", entry)
            items.append(
                InvalidDeviceEntry(
                    raw={"entry": entry},
                    reason="Missing 'name' or 'device_type' in devices.json entry",
                )
            )
            continue

        name = entry.get("name")
        device_type = entry.get("device_type")
        if not name or not device_type:
            # Mirrors the "skipped" branch the Flask app already had.
            logger.warning("Skipping device, missing 'name' or 'device_type': %s", entry)
            items.append(
                InvalidDeviceEntry(
                    raw=entry,
                    reason="Missing 'name' or 'device_type' in devices.json entry",
                )
            )
            continue

        normalised = str(device_type).upper()
        if normalised not in KNOWN_DEVICE_TYPES:
            # Not fatal: device_type no longer selects any behaviour, it only
            # labels log and error messages. python-kasa negotiates the protocol
            # with the device itself, so a model released after this list was
            # written works without a code change.
            logger.warning(
                "Device '%s' has an unfamiliar device_type '%s'. Continuing: "
                "the protocol is negotiated with the device. Known types: %s",
                name,
                device_type,
                ", ".join(KNOWN_DEVICE_TYPES),
            )

        if name in seen:
            raise ConfigError(f"Duplicate device name: '{name}'")
        seen.add(name)

        if "/" in name:
            # A '/' cannot survive a path segment: percent-encoding is undone
            # before routing. Such a device is addressed by its slug instead.
            slug = slugify(name)
            if slug:
                logger.info(
                    "Device '%s' contains a '/', so address it as '%s' on the "
                    "/devices routes.",
                    name,
                    slug,
                )
            else:
                logger.warning(
                    "Device '%s' contains a '/' and has no ASCII characters to "
                    "build a slug from, so it cannot be reached on the /devices "
                    "routes. It still appears in /get_all_device_power.",
                    name,
                )

        ip_addr = entry.get("ip_addr")
        if not ip_addr:
            raise ConfigError(f"Device '{name}' has no 'ip_addr'")

        device = DeviceEntry(
            name=name,
            device_type=normalised,
            ip_addr=str(ip_addr),
            substract=entry.get("substract"),
        )
        parsed.append(device)
        items.append(device)

    if not parsed:
        raise ConfigError("No usable devices found in the configuration file")

    return tuple(parsed), tuple(items)


def load_config(path: Path | None = None) -> Config:
    """Load and validate the configuration. Raises ConfigError on any problem."""
    resolved = path or find_config_path()
    raw = _read_json(resolved)
    if not isinstance(raw, dict):
        raise ConfigError(f"The configuration in {resolved} must be a JSON object")

    _reject_legacy_config(raw, resolved)

    email, password = _parse_credentials(raw)
    api_keys = _parse_api_keys(raw)
    devices, items = _parse_devices(raw)

    if not api_keys:
        logger.warning(
            "No API keys configured: every authenticated route will answer 403. "
            "Add 'server.api_keys' to %s or set TAPO_API_KEYS to enable them. "
            "/get_all_device_power is unaffected.",
            resolved,
        )

    logger.info("Loaded %d device(s) from %s.", len(devices), resolved)

    return Config(
        path=resolved,
        email=email,
        password=password,
        devices=devices,
        api_keys=api_keys,
        items=items,
    )
