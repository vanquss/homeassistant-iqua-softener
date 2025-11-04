from abc import ABC, abstractmethod
from datetime import datetime, timedelta
import logging
from typing import Optional, Any
import asyncio
import aiohttp
import json

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
    CoordinatorEntity,
)

from iqua_softener import (
    IquaSoftener,
    IquaSoftenerData,
    IquaSoftenerVolumeUnit,
    IquaSoftenerException,
)

from homeassistant import config_entries, core
from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
    SensorEntityDescription,
)
from homeassistant.const import PERCENTAGE
from homeassistant.const import UnitOfVolume

from .const import (
    DOMAIN,
    CONF_DEVICE_SERIAL_NUMBER,
    DEFAULT_UPDATE_INTERVAL,
    VOLUME_FLOW_RATE_LITERS_PER_MINUTE,
    VOLUME_FLOW_RATE_GALLONS_PER_MINUTE,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
):
    config = hass.data[DOMAIN][config_entry.entry_id]
    if config_entry.options:
        config.update(config_entry.options)
    device_serial_number = config[CONF_DEVICE_SERIAL_NUMBER]

    # Use the shared coordinator from __init__.py
    coordinator = config["coordinator"]
    await coordinator.async_config_entry_first_refresh()

    sensors = [
        clz(coordinator, device_serial_number, entity_description)
        for clz, entity_description in (
            (
                IquaSoftenerStateSensor,
                SensorEntityDescription(key="State", name="State"),
            ),
            (
                IquaSoftenerDeviceDateTimeSensor,
                SensorEntityDescription(
                    key="DATE_TIME",
                    name="Date/time",
                    icon="mdi:clock",
                ),
            ),
            (
                IquaSoftenerLastRegenerationSensor,
                SensorEntityDescription(
                    key="LAST_REGENERATION",
                    name="Last regeneration",
                    device_class=SensorDeviceClass.TIMESTAMP,
                ),
            ),
            (
                IquaSoftenerOutOfSaltEstimatedDaySensor,
                SensorEntityDescription(
                    key="OUT_OF_SALT_ESTIMATED_DAY",
                    name="Out of salt estimated day",
                    device_class=SensorDeviceClass.TIMESTAMP,
                ),
            ),
            (
                IquaSoftenerSaltLevelSensor,
                SensorEntityDescription(
                    key="SALT_LEVEL",
                    name="Salt level",
                    state_class=SensorStateClass.MEASUREMENT,
                    native_unit_of_measurement=PERCENTAGE,
                ),
            ),
            (
                IquaSoftenerAvailableWaterSensor,
                SensorEntityDescription(
                    key="AVAILABLE_WATER",
                    name="Available water",
                    state_class=SensorStateClass.TOTAL,
                    device_class=SensorDeviceClass.WATER,
                    icon="mdi:water",
                ),
            ),
            (
                IquaSoftenerWaterCurrentFlowSensor,
                SensorEntityDescription(
                    key="WATER_CURRENT_FLOW",
                    name="Water current flow",
                    state_class=SensorStateClass.MEASUREMENT,
                    icon="mdi:water-pump",
                ),
            ),
            (
                IquaSoftenerWaterUsageTodaySensor,
                SensorEntityDescription(
                    key="WATER_USAGE_TODAY",
                    name="Today water usage",
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    device_class=SensorDeviceClass.WATER,
                    icon="mdi:water-minus",
                ),
            ),
            (
                IquaSoftenerWaterUsageDailyAverageSensor,
                SensorEntityDescription(
                    key="WATER_USAGE_DAILY_AVERAGE",
                    name="Water usage daily average",
                    state_class=SensorStateClass.MEASUREMENT,
                    icon="mdi:water-circle",
                ),
            ),
            (
                IquaSoftenerWaterShutoffValveStateSensor,
                SensorEntityDescription(
                    key="WATER_SHUTOFF_VALVE_STATE",
                    name="Water shutoff valve state",
                    icon="mdi:valve",
                ),
            ),
        )
    ]
    async_add_entities(sensors)


class IquaSoftenerCoordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass: core.HomeAssistant,
        iqua_softener: IquaSoftener,
        update_interval_minutes: int = DEFAULT_UPDATE_INTERVAL,
        enable_websocket: bool = True,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="Iqua Softener",
            update_interval=timedelta(minutes=update_interval_minutes),
        )
        self._iqua_softener = iqua_softener
        self._enable_websocket = enable_websocket
        self._websocket_task = None
        self._websocket_session = None
        self._websocket_uri = None
        _LOGGER.info(
            "IquaSoftenerCoordinator initialized with %d minute update interval, WebSocket: %s",
            update_interval_minutes,
            enable_websocket,
        )

    async def async_start_websocket(self):
        """Start the WebSocket connection for real-time data."""
        if not self._enable_websocket:
            _LOGGER.info("WebSocket disabled, skipping connection")
            return

        try:
            # Get the WebSocket URI from the softener
            self._websocket_uri = await self.hass.async_add_executor_job(
                self._iqua_softener.get_websocket_uri
            )
            _LOGGER.info("Starting WebSocket connection to: %s", self._websocket_uri)

            # Start the WebSocket task
            self._websocket_task = self.hass.async_create_task(
                self._websocket_handler()
            )
        except Exception as err:
            _LOGGER.error("Failed to start WebSocket connection: %s", err)

    async def async_stop_websocket(self):
        """Stop the WebSocket connection."""
        if self._websocket_task:
            self._websocket_task.cancel()
            try:
                await self._websocket_task
            except asyncio.CancelledError:
                pass
            self._websocket_task = None

        if self._websocket_session:
            await self._websocket_session.close()
            self._websocket_session = None

        _LOGGER.info("WebSocket connection stopped")

    async def _websocket_handler(self):
        """Handle WebSocket connection and real-time data updates."""
        while True:
            try:
                if not self._websocket_session:
                    self._websocket_session = aiohttp.ClientSession()

                async with self._websocket_session.ws_connect(
                    self._websocket_uri,
                    timeout=aiohttp.ClientTimeout(total=30),
                    heartbeat=30,
                ) as ws:
                    _LOGGER.info("WebSocket connected successfully")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._handle_realtime_data(data)
                            except json.JSONDecodeError as err:
                                _LOGGER.warning("Invalid JSON received: %s", err)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            _LOGGER.error("WebSocket error: %s", ws.exception())
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSE:
                            _LOGGER.info("WebSocket closed by server")
                            break

            except asyncio.CancelledError:
                _LOGGER.info("WebSocket handler cancelled")
                break
            except Exception as err:
                _LOGGER.error("WebSocket connection error: %s", err)

            # Wait before reconnecting
            await asyncio.sleep(30)

    async def _handle_realtime_data(self, data):
        """Handle real-time data updates from WebSocket."""
        try:
            # Fix water flow unit conversion - use converted_property.value instead of raw value
            # Handle both direct property access and the name-based structure from WebSocket
            flow_data = None

            # Check for direct property structure
            if "current_water_flow_gpm" in data:
                flow_data = data["current_water_flow_gpm"]
            # Check for name-based structure from WebSocket
            elif (
                isinstance(data, dict) and data.get("name") == "current_water_flow_gpm"
            ):
                flow_data = data

            if flow_data and "converted_property" in flow_data:
                if "value" in flow_data["converted_property"]:
                    # Use the properly converted value from converted_property
                    corrected_value = flow_data["converted_property"]["value"]
                    original_value = flow_data.get("value", "unknown")

                    # Update the data structure for the library
                    if "current_water_flow_gpm" in data:
                        data["current_water_flow_gpm"]["value"] = corrected_value
                    else:
                        # Create the expected structure if we got the name-based format
                        data = {
                            "current_water_flow_gpm": {
                                "value": corrected_value,
                                "converted_property": flow_data["converted_property"],
                            }
                        }

                    _LOGGER.debug(
                        "Corrected water flow: raw=%s -> converted=%s gal/m",
                        original_value,
                        corrected_value,
                    )

            # Update the softener with real-time data
            await self.hass.async_add_executor_job(
                self._iqua_softener.update_external_realtime_data, data
            )

            # Trigger coordinator update to refresh all entities
            await self.async_request_refresh()

            _LOGGER.debug("Real-time data updated: %s", data)
        except Exception as err:
            _LOGGER.error("Failed to handle real-time data: %s", err)

    async def _async_update_data(self) -> IquaSoftenerData:
        _LOGGER.debug("Fetching data from iQua API")
        try:
            data = await self.hass.async_add_executor_job(
                lambda: self._iqua_softener.get_data()
            )
            _LOGGER.debug("Successfully fetched data from iQua API")
            return data
        except IquaSoftenerException as err:
            _LOGGER.error("Get data failed: %s", err)
            raise UpdateFailed(f"Get data failed: {err}")


class IquaSoftenerSensor(SensorEntity, CoordinatorEntity, ABC):
    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_serial_number: str,
        entity_description: SensorEntityDescription = None,
    ):
        super().__init__(coordinator)
        self._device_serial_number = device_serial_number
        self._attr_unique_id = (
            f"{device_serial_number}_{entity_description.key}".lower()
        )

        if entity_description is not None:
            self.entity_description = entity_description

    @callback
    def _handle_coordinator_update(self) -> None:
        self.update(self.coordinator.data)
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._device_serial_number)},
            "name": f"Iqua Softener {self._device_serial_number}",
            "manufacturer": "Iqua",
            "model": "Water Softener",
        }

    @abstractmethod
    def update(self, data: IquaSoftenerData): ...


class IquaSoftenerStateSensor(IquaSoftenerSensor):
    def update(self, data: IquaSoftenerData):
        self._attr_native_value = str(data.state.value)


class IquaSoftenerDeviceDateTimeSensor(IquaSoftenerSensor):
    def update(self, data: IquaSoftenerData):
        self._attr_native_value = data.device_date_time.strftime("%Y-%m-%d %H:%M:%S")


class IquaSoftenerLastRegenerationSensor(IquaSoftenerSensor):
    def update(self, data: IquaSoftenerData):
        self._attr_native_value = (
            datetime.now(data.device_date_time.tzinfo)
            - timedelta(days=data.days_since_last_regeneration)
        ).replace(hour=0, minute=0, second=0)


class IquaSoftenerOutOfSaltEstimatedDaySensor(IquaSoftenerSensor):
    def update(self, data: IquaSoftenerData):
        self._attr_native_value = (
            datetime.now(data.device_date_time.tzinfo)
            + timedelta(days=data.out_of_salt_estimated_days)
        ).replace(hour=0, minute=0, second=0)


class IquaSoftenerSaltLevelSensor(IquaSoftenerSensor):
    def update(self, data: IquaSoftenerData):
        self._attr_native_value = data.salt_level_percent

    @property
    def icon(self) -> Optional[str]:
        if self._attr_native_value is not None:
            if self._attr_native_value > 75:
                return "mdi:signal-cellular-3"
            elif self._attr_native_value > 50:
                return "mdi:signal-cellular-2"
            elif self._attr_native_value > 25:
                return "mdi:signal-cellular-1"
            elif self._attr_native_value > 5:
                return "mdi:signal-cellular-outline"
            return "mdi:signal-off"
        else:
            return "mdi:signal"


class IquaSoftenerAvailableWaterSensor(IquaSoftenerSensor):
    def update(self, data: IquaSoftenerData):
        self._attr_native_value = data.total_water_available / (
            1000 if data.volume_unit == IquaSoftenerVolumeUnit.LITERS else 1
        )
        self._attr_native_unit_of_measurement = (
            UnitOfVolume.CUBIC_METERS
            if data.volume_unit == IquaSoftenerVolumeUnit.LITERS
            else UnitOfVolume.GALLONS
        )
        self._attr_last_reset = datetime.now(data.device_date_time.tzinfo) - timedelta(
            days=data.days_since_last_regeneration
        )


class IquaSoftenerWaterCurrentFlowSensor(IquaSoftenerSensor):
    def update(self, data: IquaSoftenerData):
        self._attr_native_value = data.current_water_flow
        self._attr_native_unit_of_measurement = (
            VOLUME_FLOW_RATE_LITERS_PER_MINUTE
            if data.volume_unit == IquaSoftenerVolumeUnit.LITERS
            else VOLUME_FLOW_RATE_GALLONS_PER_MINUTE
        )


class IquaSoftenerWaterUsageTodaySensor(IquaSoftenerSensor):
    def update(self, data: IquaSoftenerData):
        self._attr_native_value = data.today_use / (
            1000 if data.volume_unit == IquaSoftenerVolumeUnit.LITERS else 1
        )
        self._attr_native_unit_of_measurement = (
            UnitOfVolume.CUBIC_METERS
            if data.volume_unit == IquaSoftenerVolumeUnit.LITERS
            else UnitOfVolume.GALLONS
        )


class IquaSoftenerWaterUsageDailyAverageSensor(IquaSoftenerSensor):
    def update(self, data: IquaSoftenerData):
        self._attr_native_value = data.average_daily_use / (
            1000 if data.volume_unit == IquaSoftenerVolumeUnit.LITERS else 1
        )
        self._attr_native_unit_of_measurement = (
            UnitOfVolume.CUBIC_METERS
            if data.volume_unit == IquaSoftenerVolumeUnit.LITERS
            else UnitOfVolume.GALLONS
        )


class IquaSoftenerWaterShutoffValveStateSensor(IquaSoftenerSensor):
    def update(self, data: IquaSoftenerData):
        if hasattr(data, "water_shutoff_valve_state"):
            # Convert numeric state to text
            valve_state = data.water_shutoff_valve_state
            if valve_state == 1:
                self._attr_native_value = "Open"
            elif valve_state == 0:
                self._attr_native_value = "Closed"
            else:
                self._attr_native_value = f"Unknown ({valve_state})"
        else:
            self._attr_native_value = "Unknown"

    @property
    def icon(self) -> Optional[str]:
        if self._attr_native_value == "Open":
            return "mdi:valve-open"
        elif self._attr_native_value == "Closed":
            return "mdi:valve-closed"
        else:
            return "mdi:valve"
