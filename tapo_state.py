"""Shared service state: the loaded configuration and the connected devices.

Held on `app.state.service` so both routers reach it without module globals.
"""

from __future__ import annotations

import asyncio
import logging

from tapo_config import Config, ConfigError, load_config
from tapo_devices import DeviceRegistry

logger = logging.getLogger(__name__)


class ServiceState:
    """Configuration plus device registry, swappable by /reload-config.

    A configuration failure is never fatal: the service still starts, records
    the problem in `init_error`, and reports it from /get_all_device_power.
    """

    def __init__(self) -> None:
        self.config: Config | None = None
        self.registry = DeviceRegistry()
        self.init_error: str | None = None
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self.config is not None and self.init_error is None

    async def load(self, *, connect: bool = True) -> None:
        """Load the configuration and connect. Records errors instead of raising."""
        async with self._lock:
            try:
                config = load_config()
            except ConfigError as err:
                self.init_error = str(err)
                logger.error("%s", err)
                return

            previous = self.registry
            self.config = config
            self.registry = DeviceRegistry.from_config(config)
            self.init_error = None

            await previous.disconnect_all()
            if connect:
                await self.registry.connect_all()

    async def reload(self) -> None:
        """Re-read the configuration. Raises ConfigError, leaving the old state intact."""
        async with self._lock:
            config = load_config()

            previous = self.registry
            self.config = config
            self.registry = DeviceRegistry.from_config(config)
            self.init_error = None

            await previous.disconnect_all()
            await self.registry.connect_all()

    async def shutdown(self) -> None:
        await self.registry.disconnect_all()
