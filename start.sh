#!/bin/sh
# Serve the API. Devices are contacted directly from this process; there is no
# longer a tapo-rest sidecar to launch first.
exec uvicorn taposc:app --host 0.0.0.0 --port "${TAPOSC_PORT:-5000}"
