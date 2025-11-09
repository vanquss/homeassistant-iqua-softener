from abc import ABC, abstractmethod
from datetime import datetime, timedelta
import logging
from typing import Optional, Any
import asyncio
import aiohttp
import json
import time

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
    CoordinatorEntity,
)

from .vendor.iqua_softener import (
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
        config_data: dict = None,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="Iqua Softener",
            update_interval=timedelta(minutes=update_interval_minutes),
        )
        self._iqua_softener = iqua_softener
        self._enable_websocket = enable_websocket
        self._config_data = config_data or {}

        # Store credentials for authentication recovery
        self._username = self._config_data.get("username")
        self._password = self._config_data.get("password")
        self._device_serial_number = self._config_data.get("device_sn")

        # Hybrid WebSocket: Library handles connection, HA handles real-time updates
        self._ha_websocket_task = None
        self._ha_websocket_session = None
        self._last_realtime_update = None
        self._last_api_update = None  # Track API updates separately
        
        # Flag to delay WebSocket start until after bootstrap
        self._websocket_start_delayed = False

        _LOGGER.info(
            "IquaSoftenerCoordinator initialized with %d minute update interval, WebSocket: %s (hybrid mode)",
            update_interval_minutes,
            enable_websocket,
        )

    async def async_start_websocket(self):
        """Start hybrid WebSocket: Library handles connection, HA handles real-time updates."""
        if not self._enable_websocket:
            _LOGGER.info("WebSocket disabled, skipping connection")
            return

        try:
            # Step 1: Start library's WebSocket (handles 170-second refresh, etc.)
            _LOGGER.info("🚀 Starting library WebSocket for data management...")
            await self.hass.async_add_executor_job(self._iqua_softener.start_websocket)
            _LOGGER.info("✅ Library WebSocket started successfully")
            
            # Step 2: Start lightweight HA WebSocket listener for real-time updates
            _LOGGER.info("🚀 Starting HA WebSocket listener for real-time sensor updates...")
            await self._start_ha_websocket_listener()
            _LOGGER.info("✅ Hybrid WebSocket system fully operational")
            
        except Exception as err:
            _LOGGER.error("❌ Failed to start hybrid WebSocket: %s", err)

    async def _start_ha_websocket_listener(self):
        """Start lightweight HA WebSocket listener to trigger sensor updates."""
        if self._ha_websocket_task:
            _LOGGER.debug("HA WebSocket listener already running, skipping start")
            return  # Already running
            
        try:
            # Get WebSocket URI from library
            _LOGGER.debug("Getting WebSocket URI from library...")
            websocket_uri = await self.hass.async_add_executor_job(
                self._iqua_softener.get_websocket_uri
            )
            
            if not websocket_uri:
                _LOGGER.error("❌ No WebSocket URI available for HA listener")
                return
                
            _LOGGER.info("✅ Got WebSocket URI, starting HA listener task...")
            self._ha_websocket_task = self.hass.async_create_background_task(
                self._ha_websocket_listener(websocket_uri),
                name=f"iqua_ha_websocket_{self._device_serial_number}",
            )
            _LOGGER.info("✅ HA WebSocket listener task created successfully")
            
        except Exception as err:
            _LOGGER.error("❌ Failed to start HA WebSocket listener: %s", err)

    async def _ha_websocket_listener(self, websocket_uri: str):
        """Lightweight WebSocket listener that triggers sensor updates on data changes."""
        retry_count = 0
        max_retries = 3
        
        _LOGGER.info("🌐 HA WebSocket listener starting with URI: %s", websocket_uri[:50] + "...")
        
        while retry_count < max_retries:
            try:
                if not self._ha_websocket_session:
                    self._ha_websocket_session = aiohttp.ClientSession()

                _LOGGER.debug("🌐 Connecting HA WebSocket listener (attempt %d)...", retry_count + 1)
                
                async with self._ha_websocket_session.ws_connect(
                    websocket_uri,
                    timeout=aiohttp.ClientTimeout(total=30, connect=15),
                    heartbeat=30,
                ) as ws:
                    _LOGGER.info("🌐 ✅ HA WebSocket listener connected successfully")
                    retry_count = 0  # Reset on successful connection
                    
                    message_count = 0
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            message_count += 1
                            try:
                                data = json.loads(msg.data)
                                _LOGGER.debug("🌐 WebSocket message #%d: %s", 
                                             message_count, data.get('name', 'unknown'))
                                await self._handle_realtime_message(data)
                            except json.JSONDecodeError:
                                _LOGGER.debug("🌐 Invalid JSON in WebSocket message, skipping")
                                continue  # Skip invalid JSON
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            _LOGGER.warning("🌐 ⚠️ HA WebSocket listener error: %s", ws.exception())
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSE:
                            _LOGGER.info("🌐 ℹ️ HA WebSocket listener closed by server")
                            break
                            
            except asyncio.CancelledError:
                _LOGGER.info("🌐 🛑 HA WebSocket listener cancelled")
                break
            except Exception as err:
                retry_count += 1
                _LOGGER.warning("🌐 ⚠️ HA WebSocket listener error (attempt %d/%d): %s", 
                               retry_count, max_retries, err)
                if retry_count < max_retries:
                    wait_time = min(60 * retry_count, 300)
                    _LOGGER.debug("🌐 ⏳ Waiting %d seconds before retry...", wait_time)
                    await asyncio.sleep(wait_time)
                else:
                    _LOGGER.error("🌐 ❌ HA WebSocket listener max retries reached")
                    break
                    
        _LOGGER.info("🌐 🏁 HA WebSocket listener task ending")

    async def _handle_realtime_message(self, data: dict):
        """Handle real-time WebSocket messages by triggering sensor updates."""
        _LOGGER.debug("🌐 WebSocket message received: %s", data.get('name', 'unknown'))
        
        # Only trigger updates for water flow changes (most important for real-time)
        if isinstance(data, dict) and data.get("name") == "current_water_flow_gpm":
            current_time = time.time()
            
            # Throttle updates to prevent excessive sensor refreshes (max every 5 seconds)
            if (self._last_realtime_update is None or 
                current_time - self._last_realtime_update > 5):
                
                flow_value = None
                if ("converted_property" in data and 
                    "value" in data["converted_property"]):
                    flow_value = data["converted_property"]["value"]
                else:
                    flow_value = data.get("value", "unknown")
                
                self._last_realtime_update = current_time
                _LOGGER.info("🌊 ✅ Real-time flow update detected (value: %s), triggering sensor refresh from WebSocket data...", 
                           flow_value)
                
                # Trigger coordinator refresh to update all sensors
                await self.async_request_refresh()
                
                _LOGGER.debug("🌊 ✅ Sensor refresh triggered - all sensors should now update with WebSocket indicator")
            else:
                time_since_last = current_time - self._last_realtime_update
                _LOGGER.debug("🌊 ⏳ Real-time flow update throttled (%.1f seconds since last update)", 
                             time_since_last)
        else:
            _LOGGER.debug("🌐 ℹ️ Non-water-flow WebSocket message ignored: %s", 
                         data.get("name", "unknown"))

    async def async_restart_websocket(self):
        """Restart the hybrid WebSocket connection."""
        _LOGGER.info("Restarting hybrid WebSocket connection")
        await self.async_stop_websocket()
        await self.async_start_websocket()

    async def async_stop_websocket(self):
        """Stop the hybrid WebSocket connection."""
        try:
            # Stop HA WebSocket listener
            if self._ha_websocket_task:
                self._ha_websocket_task.cancel()
                try:
                    await self._ha_websocket_task
                except asyncio.CancelledError:
                    pass
                self._ha_websocket_task = None

            if self._ha_websocket_session:
                await self._ha_websocket_session.close()
                self._ha_websocket_session = None

            # Stop library WebSocket
            _LOGGER.info("Stopping library WebSocket...")
            await self.hass.async_add_executor_job(self._iqua_softener.stop_websocket)
            _LOGGER.info("Hybrid WebSocket stopped")
        except Exception as err:
            _LOGGER.error("Failed to stop hybrid WebSocket: %s", err)

    async def async_retry_websocket(self):
        """Manually retry hybrid WebSocket connection."""
        _LOGGER.info("Manual hybrid WebSocket retry requested")
        await self.async_restart_websocket()

    async def async_force_update(self):
        """Force an immediate data update and sensor refresh."""
        _LOGGER.info("🔄 Manual data refresh requested - forcing API call and sensor updates")
        try:
            await self.async_refresh()
            _LOGGER.info("✅ Manual data refresh completed successfully")
        except Exception as err:
            _LOGGER.error("❌ Manual data refresh failed: %s", err)

    def get_websocket_status(self) -> dict:
        """Get current WebSocket status for debugging."""
        current_time = time.time()
        
        # Check library WebSocket status
        library_ws_status = "Unknown"
        try:
            # This might not be available in all library versions
            if hasattr(self._iqua_softener, '_websocket_task'):
                library_ws_status = "Running" if self._iqua_softener._websocket_task else "Stopped"
        except:
            library_ws_status = "Unknown"

        # Check HA WebSocket status  
        ha_ws_status = "Stopped"
        if self._ha_websocket_task:
            if self._ha_websocket_task.done():
                ha_ws_status = "Finished/Error"
            else:
                ha_ws_status = "Running"

        status = {
            "websocket_enabled": self._enable_websocket,
            "library_websocket": library_ws_status,
            "ha_websocket_listener": ha_ws_status,
            "last_realtime_update": self._last_realtime_update,
            "last_api_update": self._last_api_update,
            "realtime_age_seconds": current_time - self._last_realtime_update if self._last_realtime_update else None,
            "api_age_seconds": current_time - self._last_api_update if self._last_api_update else None,
        }
        
        _LOGGER.info("📊 WebSocket Status: %s", status)
        return status

    async def _async_update_data(self) -> IquaSoftenerData:
        _LOGGER.debug("📡 Starting API data fetch from iQua Softener")
        
        # Start WebSocket after first successful data fetch (post-bootstrap)
        if (self._enable_websocket and 
            not self._websocket_start_delayed):
            _LOGGER.info("🚀 Starting hybrid WebSocket after bootstrap completion...")
            self._websocket_start_delayed = True
            # Schedule WebSocket start as a background task to avoid blocking data fetch
            self.hass.async_create_task(self.async_start_websocket())
        
        try:
            data = await self.hass.async_add_executor_job(
                lambda: self._iqua_softener.get_data()
            )
            # Mark API update timestamp
            self._last_api_update = time.time()
            
            _LOGGER.info("📡 ✅ Successfully fetched API data from iQua Softener - sensors will update from API data")
            _LOGGER.debug("📡 API data contains: state=%s, flow=%s, salt=%s%%, today_use=%s", 
                         data.state.value if hasattr(data.state, 'value') else data.state,
                         data.current_water_flow, 
                         data.salt_level_percent,
                         data.today_use)
            return data
        except TypeError as err:
            # Handle library authentication issues
            if "'str' object is not callable" in str(err):
                _LOGGER.error(
                    "📡 ❌ iQua library authentication error during API fetch - may indicate library version issue: %s",
                    err,
                )
                # Try to recreate the iqua client to reset authentication state
                try:
                    _LOGGER.debug(
                        "🔄 Attempting to recreate iQua client to reset authentication"
                    )
                    from .vendor.iqua_softener import IquaSoftener

                    self._iqua_softener = IquaSoftener(
                        self._username,
                        self._password,
                        self._device_serial_number,
                    )
                    # Try the request again with fresh client
                    data = await self.hass.async_add_executor_job(
                        lambda: self._iqua_softener.get_data()
                    )
                    # Mark API update timestamp
                    self._last_api_update = time.time()
                    
                    _LOGGER.info("📡 ✅ Successfully recovered from authentication error - API data fetched")

                    # Also restart WebSocket with fresh client if enabled
                    if self._enable_websocket:
                        _LOGGER.debug("🔄 Restarting hybrid WebSocket after main auth recovery")
                        self.hass.async_create_task(self.async_restart_websocket())

                    return data
                except Exception as recovery_err:
                    _LOGGER.error(
                        "📡 ❌ Failed to recover from authentication error: %s", recovery_err
                    )
                    raise UpdateFailed(f"iQua library authentication error: {err}")
            else:
                _LOGGER.error("📡 ❌ Unexpected TypeError in iQua API call: %s", err)
                raise UpdateFailed(f"Unexpected error: {err}")
        except IquaSoftenerException as err:
            _LOGGER.error("📡 ❌ API data fetch failed: %s", err)
            raise UpdateFailed(f"Get data failed: {err}")
        except Exception as err:
            _LOGGER.error("📡 ❌ Unexpected error fetching API data: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}")


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
        
        # Track update sources for debugging
        self._last_update_source = None
        self._last_update_time = None

        if entity_description is not None:
            self.entity_description = entity_description

    @callback
    def _handle_coordinator_update(self) -> None:
        # Determine update source for debugging
        current_time = time.time()
        update_source = "API"
        
        # Check if this update was triggered by WebSocket (within 10 seconds)
        if (hasattr(self.coordinator, '_last_realtime_update') and 
            self.coordinator._last_realtime_update and 
            current_time - self.coordinator._last_realtime_update < 10):
            update_source = "WebSocket"
        # Check if this was triggered by API update (within 5 seconds)
        elif (hasattr(self.coordinator, '_last_api_update') and 
              self.coordinator._last_api_update and 
              current_time - self.coordinator._last_api_update < 5):
            update_source = "API"
        else:
            # Fallback - check which was more recent
            ws_age = float('inf')
            api_age = float('inf')
            
            if (hasattr(self.coordinator, '_last_realtime_update') and 
                self.coordinator._last_realtime_update):
                ws_age = current_time - self.coordinator._last_realtime_update
                
            if (hasattr(self.coordinator, '_last_api_update') and 
                self.coordinator._last_api_update):
                api_age = current_time - self.coordinator._last_api_update
                
            if ws_age < api_age and ws_age < 60:  # WebSocket within last minute
                update_source = "WebSocket"
            elif api_age < 300:  # API within last 5 minutes  
                update_source = "API"
            else:
                update_source = "Unknown"
            
        self._last_update_source = update_source
        self._last_update_time = current_time
        
        _LOGGER.debug("🔄 %s sensor updating from %s", 
                     self.entity_description.name, update_source)
        
        self.update(self.coordinator.data)
        self.async_write_ha_state()
        
        _LOGGER.debug("✅ %s sensor updated from %s, value: %s", 
                     self.entity_description.name, update_source, 
                     getattr(self, '_attr_native_value', 'N/A'))

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
        old_value = getattr(self, '_attr_native_value', None)
        self._attr_native_value = str(data.state.value)
        
        if old_value != self._attr_native_value:
            _LOGGER.info("🔄 State sensor changed: %s → %s", old_value, self._attr_native_value)
        else:
            _LOGGER.debug("📊 State sensor unchanged: %s", self._attr_native_value)


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
        old_value = getattr(self, '_attr_native_value', None)
        self._attr_native_value = data.salt_level_percent
        
        if old_value != self._attr_native_value:
            _LOGGER.info("🧂 Salt level sensor changed: %s%% → %s%%", old_value, self._attr_native_value)
        else:
            _LOGGER.debug("📊 Salt level sensor unchanged: %s%%", self._attr_native_value)

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
        # Enhanced debug logging to diagnose WebSocket flow data issues
        _LOGGER.debug("=== Water Flow Sensor Update Debug ===")
        _LOGGER.debug("📡 API current_water_flow value: %s", data.current_water_flow)
        
        # Use the library's get_realtime_property method for real-time flow data
        realtime_flow = self.coordinator._iqua_softener.get_realtime_property(
            "current_water_flow_gpm"
        )
        
        _LOGGER.debug("🌐 Library realtime flow value: %s", realtime_flow)
        
        # Determine data source and value to use
        data_source = "API"
        if realtime_flow is not None:
            # Use real-time WebSocket data
            self._attr_native_value = realtime_flow
            data_source = "WebSocket/Library"
            _LOGGER.info("🌊 ✅ Water flow sensor using REAL-TIME data from %s: %s", 
                        data_source, realtime_flow)
        else:
            # Fall back to regular API data
            self._attr_native_value = data.current_water_flow
            _LOGGER.info("🌊 ⚠️ Water flow sensor using API data (no realtime available): %s", 
                        self._attr_native_value)

        self._attr_native_unit_of_measurement = (
            VOLUME_FLOW_RATE_LITERS_PER_MINUTE
            if data.volume_unit == IquaSoftenerVolumeUnit.LITERS
            else VOLUME_FLOW_RATE_GALLONS_PER_MINUTE
        )
        
        _LOGGER.debug("🌊 Final flow: %s %s (source: %s)", 
                     self._attr_native_value, 
                     self._attr_native_unit_of_measurement,
                     data_source)
        _LOGGER.debug("=== End Water Flow Sensor Update ===")


class IquaSoftenerWaterUsageTodaySensor(IquaSoftenerSensor):
    def update(self, data: IquaSoftenerData):
        old_value = getattr(self, '_attr_native_value', None)
        self._attr_native_value = data.today_use / (
            1000 if data.volume_unit == IquaSoftenerVolumeUnit.LITERS else 1
        )
        self._attr_native_unit_of_measurement = (
            UnitOfVolume.CUBIC_METERS
            if data.volume_unit == IquaSoftenerVolumeUnit.LITERS
            else UnitOfVolume.GALLONS
        )
        
        if old_value != self._attr_native_value:
            _LOGGER.info("💧 Today's water usage sensor changed: %s → %s %s", 
                        old_value, self._attr_native_value, self._attr_native_unit_of_measurement)
        else:
            _LOGGER.debug("📊 Today's water usage sensor unchanged: %s %s", 
                         self._attr_native_value, self._attr_native_unit_of_measurement)


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