# Tapo Device Power REST API

A lightweight, **pure-Python** REST API for controlling Tapo smart devices and fetching their power data, packaged for Docker deployment.

My goal was to retrieve data usage for several Tapo smart plugs in 1 single response, to use in a [custom API](https://gethomepage.dev/widgets/services/customapi/) service widget in my [Homepage](https://gethomepage.dev/) dashboard:

### Syntax
```
- Power:
    - Tapo:
        icon: mdi-home-lightning-bolt-outline
        widgets:
            - type: customapi
              url:  http://<taporestsc_url>:5000/get_all_device_power
              refreshInterval: 5000 # in milliseconds
              display: dynamic-list
              mappings:
                    name: device
                    label: data.current_power
                    suffix: Watts
```
### Result
![Homepage widget](images/homepage.png)

## Features

- One endpoint returning the current power of every configured device, plus a computed total
- Chained plugs: subtract one device's consumption from another's, so nothing is counted twice
- A full REST surface for controlling and querying individual devices, compatible with [tapo-rest](https://github.com/ClementNerma/tapo-rest)'s routes
- Speaks to devices **directly over your LAN** with [python-kasa](https://github.com/python-kasa/python-kasa) — no bundled binary, no sidecar process, no cloud round-trip
- Expired device sessions are re-established transparently instead of failing the request
- Dockerized, and architecture-independent

## Quick Start

### 1. Clone this Repository

```
git clone <this-repo-url>
cd <this-repo-directory>
```

### 2. Prepare Configuration

Copy the sample and fill it in:

```
cp app/config.sample.json app/config.json
```

`app/config.json` is gitignored — it holds your Tapo credentials and API keys. See [Configuration](#configuration).

### 3. Build the Docker Image

`docker build -t taposc .`

### 4. Run the Container

The `app` directory must be mounted, since it holds the configuration:

`docker run -d -p 5000:5000 -v ./app:/app -e TZ=Europe/Brussels --name tapo taposc`

> **Run only one client against your plugs.** Tapo devices accept a single local
> session per device: whichever client authenticated most recently wins, and the
> other one starts getting `Session timeout` until it reconnects. Stop any older
> tapo-rest container before starting this one.

## Configuration

**There is exactly one configuration file: `app/config.json`.** Earlier versions
of this project used two (`config.json` for the tapo-rest URL and password, and
`devices.json` for the device list). Both are gone: `app/devices.json` is now
`app/config.json`, and the old `tapo_api_url` / `login_password` keys no longer
exist, because there is no separate tapo-rest process to point at any more.

Start from the committed template — it is a complete, working configuration:

```json
{
    "tapo_credentials": {
        "email": "your-tapo-account@example.com",
        "password": "your-tapo-account-password"
    },
    "devices": [
        { "name": "Living room plug", "device_type": "P110", "ip_addr": "192.168.1.10" },
        { "name": "Washer",           "device_type": "P115", "ip_addr": "192.168.1.11" },
        { "name": "TV corner",        "device_type": "P110", "ip_addr": "192.168.1.12",
          "substract": "Living room plug" }
    ]
}
```

| Key | Meaning |
| --- | --- |
| `tapo_credentials` | Your Tapo **account** email and password — the same ones you use in the Tapo app. Devices are contacted locally over your LAN; these only authenticate you to them. |
| `devices[].name` | Any name you like. This is what `?device=` and the widget's `device` field use. |
| `devices[].device_type` | One of `L510` `L520` `L530` `L535` `L610` `L630` `L900` `L920` `L930` `P100` `P105` `P110` `P110M` `P115` `P300` `P304` `P304M` `P316`. |
| `devices[].ip_addr` | The device's address on your LAN. Give it a DHCP reservation. |
| `devices[].substract` | Optional. The name of another configured device whose power is subtracted from this one — for plugs chained behind one another. Never goes below zero. |

### Do I need an API key?

**Probably not.** `/get_all_device_power` — the endpoint the Homepage widget
calls — is **unauthenticated**, exactly as it has always been. Your widget needs
no key, no header and no change.

An API key only guards the routes that can *change* a device or reveal your
setup: `/actions/…`, `/devices`, `/refresh-session` and `/reload-config`. Add
one only if you want to switch devices on and off over HTTP, from a script or
Home Assistant. Until you do, those routes answer `403` and everything else
works normally.

To add one, generate a key and put it in `app/config.json`:

```shell
openssl rand -hex 32
```

```json
{
    "tapo_credentials": { "…": "…" },
    "server": {
        "api_keys": [
            { "name": "Home Assistant", "key": "paste-the-generated-key-here" }
        ]
    },
    "devices": [ "…" ]
}
```

Keys must be **at least 32 characters and alphanumeric only** — no dashes or
underscores. The service refuses to start if a key does not qualify, rather than
running with a weak one.

### Environment variables

| Variable | Effect |
| --- | --- |
| `TAPO_EMAIL` / `TAPO_PASSWORD` | **Override** `tapo_credentials` in the file. See the warning below. |
| `TAPO_API_KEYS` | Comma-separated API keys, **added to** any in the file. |
| `TAPOSC_CONFIG` | Full path to the configuration file, instead of `app/config.json`. |
| `TAPOSC_PORT` | Port to listen on. Defaults to `5000`. |
| `TAPOSC_LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING`, … |
| `TZ` | The container's timezone, e.g. `Europe/Brussels`. Only affects `get-*-energy-data`, whose day and month boundaries are local ones. Without it the container runs in UTC and a "day" starts at 00:00 UTC. |

> ⚠️ **`TAPO_EMAIL` and `TAPO_PASSWORD` beat the configuration file.** If both are
> set, the environment wins and `tapo_credentials` in `config.json` is ignored.
> That bites when you rotate your Tapo password: editing the file alone changes
> nothing, and the service keeps trying the old credentials from the
> environment. The startup log says so explicitly when it happens:
>
> ```
> TAPO_EMAIL and TAPO_PASSWORD set in the environment, overriding
> 'tapo_credentials' in /app/config.json. Editing that file alone will not
> change how this service authenticates -- change the environment variable, or
> unset it to let the file win.
> ```
>
> **Pick one source and stick to it.** Either keep the credentials in
> `config.json` and set neither variable, or set both variables and leave
> `tapo_credentials` out of the file. Mixing them is what causes surprises.
>
> There is no `AUTH_PASSWORD`. It existed when this project talked to a separate
> tapo-rest process over HTTP; nothing reads it now. Delete it from your
> container configuration. Its replacement, if you want the `/actions` routes,
> is an API key — see [Do I need an API key?](#do-i-need-an-api-key).

### Upgrading from an older version of this project

Two steps, both on your mounted `app/` directory:

1. **Rename `devices.json` to `config.json`.** Its contents already have the
   right shape — `tapo_credentials`, `devices` and any `substract` keys are read
   as-is, and the old `server_password` is parsed and ignored. If you forget,
   the old filename is still read, with a warning in the log.
2. **Delete the old `config.json`** first, the one holding `tapo_api_url` and
   `login_password`. Nothing reads those keys any more, and leaving that file in
   place would shadow the renamed one. The service detects it and says so
   instead of failing obscurely.
3. **Clear out the old environment variables.** `AUTH_PASSWORD` is dead. And note
   that `TAPO_EMAIL` / `TAPO_PASSWORD`, which earlier versions of this project
   ignored, are now read and take precedence over the file — so leaving them set
   silently makes `config.json` credentials inert.

You do not need to add an API key unless you want the `/actions` routes.

## API Usage

### Aggregated power — no API key needed

This is what the Homepage widget at the top of this README calls. It takes no
`Authorization` header.

- **GET `/get_all_device_power`**
  A JSON array with one entry per configured device, in configuration order, followed by a synthetic `Total Consumption` entry.

  ```json
  [
    { "data": { "current_power": 74 }, "device": "UPS", "status": "success" },
    { "data": { "current_power": 81,
                "subtraction_info": { "adjusted_power": 81, "original_power": 155,
                                      "subtracted_device": "UPS", "subtracted_power": 74 } },
      "device": "TV", "status": "success" },
    { "data": { "current_power": 155, "included_devices": ["UPS", "TV"] },
      "device": "Total Consumption", "status": "success" }
  ]
  ```

  A device that cannot be reached gets `"status": "failed"` and is left out of the total, rather than counted as zero.

- **GET `/`** — redirects to `/get_all_device_power`.

### Device actions — API key required

All action routes take `?device=<name>`, use `GET`, and expect an `Authorization: Bearer <api key>` header:

```shell
curl -H 'Authorization: Bearer <your API key>' \
     'http://localhost:5000/actions/p115/get-current-power?device=Washer'
```

- **GET `/actions`** — the full list of available routes. The only action route that needs no key.
- **GET `/devices`** — the configured devices.
- **GET `/refresh-session?device=<name>`** — force a reconnection. Rarely needed now, since expired sessions are re-established automatically.
- **POST `/reload-config`** — re-read `config.json` without restarting.

Available actions, by device type:

| Device type | Actions |
| --- | --- |
| `L510` `L520` `L610` | `on` `off` `set-brightness` `get-device-info` `get-device-usage` |
| `L530` `L535` `L630` `L900` | the above, plus `set-color` `set-hue-saturation` `set-color-temperature` |
| `L920` `L930` | the above, plus `set-lighting-effect` |
| `P100` `P105` | `on` `off` `get-device-info` `get-device-usage` |
| `P110` `P110M` `P115` | the above, plus `get-energy-usage` `get-current-power` `get-hourly-energy-data` `get-daily-energy-data` `get-monthly-energy-data` |
| `P300` `P304` `P304M` `P316` | `get-device-info` `get-child-device-list` |

Dates in `get-*-energy-data` are `YYYY-MM-DD`. `get-hourly-energy-data` also accepts an optional `end_date`.

Errors come back as plain text with the relevant status code: `400` for a bad or missing parameter, `403` for a bad key, `404` for an unknown device, `500` when the device itself fails.

> Only the plug types are exercised against real hardware here. The bulb, light
> strip and power strip routes are implemented but untested — reports welcome.

## Project Structure

```
.
├── app/
│   ├── config.sample.json  # Committed template -- copy this
│   └── config.json         # Your real configuration (gitignored, not in the repo)
├── taposc.py               # FastAPI application entrypoint
├── tapo_config.py          # Configuration loading and validation
├── tapo_devices.py         # python-kasa device layer, action table
├── tapo_power.py           # /get_all_device_power
├── tapo_rest_api.py        # /actions, /devices, /refresh-session, /reload-config
├── tapo_state.py           # Shared config + device registry
├── tests/                  # Test suite
├── start.sh                # Entrypoint script
├── requirements.txt        # Runtime dependencies
├── requirements-dev.txt    # Test dependencies
├── Dockerfile              # Docker build instructions
└── README.md               # This file
```

## Running Locally

```shell
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn taposc:app --host 0.0.0.0 --port 5000
pytest
```

## Credits & Inspiration

- [tapo-rest](https://github.com/ClementNerma/tapo-rest) by Clément Nerma, whose REST API this project's `/actions` routes reproduce, and which earlier versions of this project bundled as a binary
- [python-kasa](https://github.com/python-kasa/python-kasa), which now does the talking to the devices

This project is not affiliated with TP-Link or Tapo.

---

## License

This project is provided as-is, with no warranty.
