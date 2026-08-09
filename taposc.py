"""Tapo Device Power REST API.

A single pure-Python service that talks to Tapo devices directly over the LAN
via python-kasa. It replaces the previously bundled `tapo-rest` binary, and
exposes both:

  * `/get_all_device_power` -- the aggregated reading a Homepage dashboard
    widget consumes, unchanged.
  * a tapo-rest compatible `/actions` surface for controlling and querying
    individual devices.

Run it with `uvicorn taposc:app --host 0.0.0.0 --port 5000`.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import tapo_power
import tapo_rest_api
from tapo_devices import ActionError, DeviceError
from tapo_rest_api import ApiError
from tapo_state import ServiceState

logging.basicConfig(
    level=os.environ.get("TAPOSC_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    service: ServiceState = app.state.service
    await service.load()
    try:
        yield
    finally:
        await service.shutdown()


app = FastAPI(
    title="Tapo Device Power REST API",
    description="Pure-Python control and power monitoring for Tapo devices.",
    lifespan=lifespan,
)
app.state.service = ServiceState()

# tapo-rest allowed any origin; keep that so existing dashboards keep working.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_exception_handler(ApiError, tapo_rest_api.api_error_handler)
app.add_exception_handler(DeviceError, tapo_rest_api.device_error_handler)
app.add_exception_handler(ActionError, tapo_rest_api.action_error_handler)

app.include_router(tapo_power.router)
app.include_router(tapo_rest_api.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("TAPOSC_PORT", "5000")),
    )
