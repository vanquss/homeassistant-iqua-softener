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
    IQUA_WEBSOCKET_BASE_URL,
    CONF_WEBSOCKET_BASE_URL,
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

        # Home Assistant managed WebSocket
        self._websocket_task = None
        self._websocket_session = None
        self._websocket_uri = None
        self._websocket_failed_permanently = False
        self._last_websocket_refresh = None
        self._websocket_refresh_interval = 150  # Refresh URI every 2.5 minutes (WebSocket times out at 3 min)
        self._realtime_data = {}  # Store real-time WebSocket data
        self._realtime_data_timestamps = (
            {}
        )  # Track when real-time data was last updated
        self._realtime_data_timeout = (
            300  # Clear real-time data after 5 minutes of no updates
        )

        # Create a unique WebSocket identifier based on device serial and account
        self._websocket_key = f"iqua_{self._iqua_softener.device_serial_number}_{hash(self._iqua_softener._username)}"

        # Flag to delay WebSocket start until after bootstrap
        self._websocket_start_delayed = False

        _LOGGER.info(
            "IquaSoftenerCoordinator initialized with %d minute update interval, WebSocket: %s (key: %s)",
            update_interval_minutes,
            enable_websocket,
            self._websocket_key,
        )

    async def async_reset_websocket_authentication(self):
        """Reset WebSocket connection due to authentication issues."""
        _LOGGER.info("Resetting WebSocket connection due to authentication issues")

        # Stop current WebSocket
        await self.async_stop_websocket()

        # Clear authentication state
        self._websocket_uri = None
        self._last_websocket_refresh = None
        self._websocket_failed_permanently = False

        # Recreate the iQua client to reset authentication
        try:
            _LOGGER.debug("Recreating iQua client for WebSocket reset")
            from iqua_softener import IquaSoftener

            self._iqua_softener = IquaSoftener(
                self._username,
                self._password,
                self._device_serial_number,
            )

            # Get fresh WebSocket URI
            await self._refresh_websocket_uri()

            # Restart WebSocket if URI was obtained
            if self._websocket_uri:
                await self.async_start_websocket()
                _LOGGER.info("WebSocket connection reset and restarted successfully")
            else:
                _LOGGER.error("Failed to get WebSocket URI after authentication reset")

        except Exception as err:
            _LOGGER.error("Failed to reset WebSocket authentication: %s", err)
            self._websocket_failed_permanently = True

    async def async_start_websocket(self):
        """Start the WebSocket connection managed by Home Assistant."""
        if not self._enable_websocket:
            _LOGGER.info("WebSocket disabled, skipping connection")
            return

        if self._websocket_failed_permanently:
            _LOGGER.info("WebSocket failed permanently, skipping reconnection attempt")
            return

        # Check if another instance already has a WebSocket for this device
        if "iqua_websockets" in self.hass.data:
            existing_ws = self.hass.data["iqua_websockets"].get(self._websocket_key)
            if existing_ws and not existing_ws.done():
                _LOGGER.info(
                    "WebSocket already active for this device, sharing connection"
                )
                self._websocket_task = existing_ws
                return

        try:
            # Get fresh WebSocket URI from the library with timeout
            _LOGGER.info("Getting WebSocket URI from library...")
            try:
                self._websocket_uri = await asyncio.wait_for(
                    self.hass.async_add_executor_job(
                        self._iqua_softener.get_websocket_uri
                    ),
                    timeout=30.0  # 30 second timeout for getting URI
                )
            except asyncio.TimeoutError:
                _LOGGER.error("Timeout getting WebSocket URI from library")
                return

            if not self._websocket_uri:
                _LOGGER.error("Failed to get WebSocket URI from library")
                return

            _LOGGER.info("Got WebSocket URI, starting connection...")

            # Start the WebSocket handler task with proper name and error handling
            self._websocket_task = self.hass.async_create_background_task(
                self._websocket_handler(),
                name=f"iqua_websocket_{self._device_serial_number}",
            )

            # Register in global WebSocket registry
            if "iqua_websockets" not in self.hass.data:
                self.hass.data["iqua_websockets"] = {}
            self.hass.data["iqua_websockets"][
                self._websocket_key
            ] = self._websocket_task

            _LOGGER.info("WebSocket connection started successfully")

        except Exception as err:
            _LOGGER.error("Failed to start WebSocket connection: %s", err)
            self._websocket_failed_permanently = True

    async def async_restart_websocket(self):
        """Restart the WebSocket connection."""
        _LOGGER.info("Restarting WebSocket connection")
        self._websocket_failed_permanently = False
        await self.async_stop_websocket()
        await self.async_start_websocket()

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

        # Remove from global WebSocket registry
        if "iqua_websockets" in self.hass.data:
            self.hass.data["iqua_websockets"].pop(self._websocket_key, None)

        # Clear real-time data when WebSocket stops
        self._realtime_data.clear()
        _LOGGER.info("WebSocket connection stopped and real-time data cleared")

    async def _websocket_handler(self):
        """Handle WebSocket connection with 3-minute refresh cycle."""
        retry_count = 0
        max_retries = 3

        _LOGGER.debug("🚀 WebSocket handler starting with 3-minute refresh cycle...")

        while retry_count < max_retries:
            try:
                # Get fresh WebSocket URI before each connection
                _LOGGER.info("🔄 Getting fresh WebSocket URI from /live endpoint...")
                try:
                    new_uri = await self.hass.async_add_executor_job(
                        self._iqua_softener.get_websocket_uri
                    )
                    if new_uri:
                        self._websocket_uri = new_uri
                        self._last_websocket_refresh = time.time()
                        _LOGGER.info("✅ Got fresh WebSocket URI for connection")
                    else:
                        _LOGGER.error("❌ Failed to get WebSocket URI from /live endpoint")
                        raise Exception("No WebSocket URI available")
                except Exception as uri_err:
                    _LOGGER.error("❌ Failed to get WebSocket URI: %s", uri_err)
                    retry_count += 1
                    await asyncio.sleep(min(60 * retry_count, 300))
                    continue

                if not self._websocket_session:
                    self._websocket_session = aiohttp.ClientSession()

                _LOGGER.info("� Connecting WebSocket (attempt %d/%d)", retry_count + 1, max_retries)

                # Add connection timeout to prevent hanging during bootstrap
                try:
                    async with asyncio.timeout(60):  # 60 second timeout for connection
                        async with self._websocket_session.ws_connect(
                            self._websocket_uri,
                            timeout=aiohttp.ClientTimeout(total=30, connect=15),
                            heartbeat=30,
                        ) as ws:
                            connection_success_time = time.time()
                            _LOGGER.info("✅ WebSocket connected successfully")
                            retry_count = 0

                            # Connection monitoring variables
                            last_message_time = time.time()
                            last_heartbeat_log = time.time()
                            message_count = 0
                            websocket_start_time = time.time()
                            
                            # Timing intervals - WebSocket URI is only valid for 3 minutes
                            heartbeat_log_interval = 60      # Log heartbeat every 1 minute
                            websocket_lifetime_limit = 150   # Disconnect and refresh after 2.5 minutes (before 3min expiry)
                            
                            _LOGGER.debug("🔍 Starting WebSocket message loop with 3-minute refresh cycle...")

                            async for msg in ws:
                                current_time = time.time()
                                
                                # Check if WebSocket has been connected for 2.5 minutes - force refresh before 3min expiry
                                websocket_age = current_time - websocket_start_time
                                if websocket_age > websocket_lifetime_limit:
                                    _LOGGER.info("� WebSocket URI expired after %.1f minutes - disconnecting for refresh", 
                                               websocket_age / 60)
                                    break  # Exit the message loop to trigger reconnection with fresh URI
                                
                                # Periodic heartbeat logging to verify connection is alive
                                if current_time - last_heartbeat_log > heartbeat_log_interval:
                                    time_since_message = current_time - last_message_time
                                    _LOGGER.info("💓 WebSocket heartbeat: %d messages, %.1f min old, %.1f min remaining", 
                                                message_count, time_since_message / 60, 
                                                (websocket_lifetime_limit - websocket_age) / 60)
                                    last_heartbeat_log = current_time

                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    last_message_time = current_time
                                    message_count += 1
                                    try:
                                        data = json.loads(msg.data)
                                        _LOGGER.debug("📨 WebSocket message #%d: %s", message_count, 
                                                     data.get('name', 'unknown') if isinstance(data, dict) else 'raw_data')
                                        await self._handle_realtime_data(data)
                                    except json.JSONDecodeError as err:
                                        _LOGGER.warning("❌ Invalid JSON received: %s", err)
                                elif msg.type == aiohttp.WSMsgType.ERROR:
                                    _LOGGER.error("❌ WebSocket error: %s", ws.exception())
                                    break
                                elif msg.type == aiohttp.WSMsgType.CLOSE:
                                    close_code = getattr(ws, 'close_code', 'unknown')
                                    _LOGGER.warning("🔌 WebSocket closed by server (code: %s)", close_code)
                                    break
                                elif msg.type == aiohttp.WSMsgType.PONG:
                                    _LOGGER.debug("🏓 WebSocket PONG received")
                                elif msg.type == aiohttp.WSMsgType.PING:
                                    _LOGGER.debug("🏓 WebSocket PING received")
                                else:
                                    _LOGGER.debug("📋 WebSocket message type: %s", msg.type)

                            # If we exit the message loop, continue the refresh cycle
                            final_time = time.time()
                            connection_duration = final_time - connection_success_time
                            _LOGGER.info("� WebSocket disconnected after %.2f minutes (%d messages) - starting refresh cycle", 
                                       connection_duration / 60, message_count)
                            # Don't increment retry_count here - this is normal operation for 3-minute refresh

                except asyncio.TimeoutError:
                    _LOGGER.error("❌ WebSocket connection timeout after 60 seconds")
                    retry_count += 1
                    continue

            except asyncio.CancelledError:
                _LOGGER.info("🛑 WebSocket handler cancelled")
                break
            except aiohttp.ClientConnectorError as err:
                _LOGGER.error("❌ WebSocket connection failed (attempt %d/%d): %s", 
                             retry_count + 1, max_retries, err)
            except aiohttp.WSServerHandshakeError as err:
                _LOGGER.error("❌ WebSocket handshake failed (attempt %d/%d): %s", 
                             retry_count + 1, max_retries, err)
            except aiohttp.ClientResponseError as err:
                if err.status == 400:
                    _LOGGER.error("❌ WebSocket 400 error - Token likely expired!")
                    if self._last_websocket_refresh:
                        token_age = time.time() - self._last_websocket_refresh
                        _LOGGER.error("   🕐 Token age at failure: %.2f minutes", token_age / 60)
                    
                    # Always try to recover from 400 errors (don't set permanent failure)
                    _LOGGER.info("🔄 Attempting to recover from token expiration...")
                    try:
                        await self._recover_from_token_expiration()
                        # Reset retry count on successful recovery to give fresh attempts
                        retry_count = 0
                        continue
                    except Exception as recovery_err:
                        _LOGGER.error("❌ Token recovery failed: %s", recovery_err)
                elif err.status == 401:
                    _LOGGER.error("❌ WebSocket 401 error - Authentication failed")
                    try:
                        await self._recover_from_token_expiration()
                        retry_count = 0
                        continue
                    except Exception as recovery_err:
                        _LOGGER.error("❌ Auth recovery failed: %s", recovery_err)
                elif err.status == 403:
                    _LOGGER.error("❌ WebSocket 403 error - Access forbidden")
                else:
                    _LOGGER.error("❌ WebSocket HTTP error %d (attempt %d/%d): %s", 
                                 err.status, retry_count + 1, max_retries, err)
            except asyncio.TimeoutError as err:
                _LOGGER.error("❌ WebSocket timeout (attempt %d/%d): %s", 
                             retry_count + 1, max_retries, err)
            except Exception as err:
                _LOGGER.error("❌ Unexpected WebSocket error (attempt %d/%d): %s", 
                             retry_count + 1, max_retries, err, exc_info=True)

            retry_count += 1
            if retry_count < max_retries:
                wait_time = min(60 * retry_count, 300)
                _LOGGER.info("⏳ Waiting %d seconds before WebSocket retry...", wait_time)
                await asyncio.sleep(wait_time)
            else:
                _LOGGER.error("❌ Max WebSocket retry attempts reached. WebSocket functionality disabled.")
                self._websocket_failed_permanently = True
                break

    async def _recover_from_token_expiration(self):
        """Attempt to recover from WebSocket token expiration."""
        _LOGGER.info("🔄 Starting token expiration recovery...")
        
        try:
            # Step 1: Recreate the client to reset authentication
            _LOGGER.debug("1️⃣ Recreating iQua client...")
            from iqua_softener import IquaSoftener
            
            self._iqua_softener = IquaSoftener(
                self._username,
                self._password,
                self._device_serial_number,
            )
            
            # Step 2: Get fresh WebSocket URI
            _LOGGER.debug("2️⃣ Getting fresh WebSocket URI...")
            new_uri = await self.hass.async_add_executor_job(
                self._iqua_softener.get_websocket_uri
            )
            
            if new_uri:
                self._websocket_uri = new_uri
                self._last_websocket_refresh = time.time()
                _LOGGER.info("✅ Token recovery successful - new URI obtained")
            else:
                _LOGGER.error("❌ Token recovery failed - no URI obtained")
                raise Exception("Failed to obtain new WebSocket URI")
                
        except Exception as err:
            _LOGGER.error("❌ Token recovery failed: %s", err)
            raise

    async def _refresh_websocket_uri(self):
        """Refresh the WebSocket URI using the library with enhanced logging."""
        try:
            current_time = time.time()

            # Enhanced logging for debugging
            _LOGGER.debug("=== WebSocket URI Refresh Debug ===")
            _LOGGER.debug("🕐 Current time: %s", datetime.fromtimestamp(current_time))
            
            if self._last_websocket_refresh:
                time_since_refresh = current_time - self._last_websocket_refresh
                _LOGGER.debug("🕐 Time since last refresh: %.2f minutes", time_since_refresh / 60)
                
                # Check if we need to refresh based on time (but allow forced refresh)
                if time_since_refresh < self._websocket_refresh_interval:
                    _LOGGER.debug("⏭️ WebSocket URI refreshed %.0f seconds ago, skipping routine refresh", 
                                 time_since_refresh)
                    return

            await self._force_refresh_websocket_uri()
                
        except Exception as err:
            _LOGGER.error("❌ Failed to refresh WebSocket URI: %s", err, exc_info=True)
            raise

    async def _force_refresh_websocket_uri(self):
        """Force refresh the WebSocket URI regardless of timing."""
        try:
            current_time = time.time()
            
            _LOGGER.info("🔄 Force refreshing WebSocket URI using library (/live endpoint call)...")
            
            # Store old URI for comparison
            old_uri = self._websocket_uri
            old_token_preview = self._get_token_preview(old_uri) if old_uri else None
            
            # Use the library to get a fresh WebSocket URI (this calls /live endpoint internally)
            new_uri = await self.hass.async_add_executor_job(
                self._iqua_softener.get_websocket_uri
            )

            if new_uri:
                new_token_preview = self._get_token_preview(new_uri)
                _LOGGER.debug("🔑 Old token: %s", old_token_preview or "None")
                _LOGGER.debug("🔑 New token: %s", new_token_preview or "None")
                
                if new_uri != self._websocket_uri:
                    self._websocket_uri = new_uri
                    self._last_websocket_refresh = current_time
                    _LOGGER.info("✅ WebSocket URI force refreshed successfully (token changed)")
                else:
                    _LOGGER.warning("⚠️ WebSocket URI force refresh returned same URI - token may not have changed")
                    # Update timestamp anyway to prevent constant refresh attempts
                    self._last_websocket_refresh = current_time
            else:
                _LOGGER.error("❌ WebSocket URI force refresh returned empty/None")
                raise Exception("Failed to get new WebSocket URI from library")
                
            _LOGGER.debug("=== End WebSocket URI Force Refresh ===")

        except TypeError as err:
            # Handle the same authentication error we saw in data fetching
            if "'str' object is not callable" in str(err):
                _LOGGER.error("🔐 Authentication error during WebSocket URI force refresh: %s", err)
                # Try to recreate the client
                try:
                    _LOGGER.debug("🔄 Recreating iQua client during WebSocket URI force refresh")
                    from iqua_softener import IquaSoftener

                    self._iqua_softener = IquaSoftener(
                        self._username,
                        self._password,
                        self._device_serial_number,
                    )
                    # Try again with fresh client
                    new_uri = await self.hass.async_add_executor_job(
                        self._iqua_softener.get_websocket_uri
                    )
                    if new_uri:
                        self._websocket_uri = new_uri
                        self._last_websocket_refresh = time.time()
                        _LOGGER.info("✅ WebSocket URI force refreshed after authentication recovery")
                    else:
                        _LOGGER.error("❌ WebSocket URI force refresh failed even after authentication recovery")
                        raise Exception("Failed to get WebSocket URI after auth recovery")
                except Exception as recovery_err:
                    _LOGGER.error("❌ Failed to recover WebSocket authentication: %s", recovery_err)
                    raise
            else:
                _LOGGER.error("❌ Unexpected TypeError during WebSocket URI force refresh: %s", err)
                raise
        except Exception as err:
            _LOGGER.error("❌ Failed to force refresh WebSocket URI: %s", err, exc_info=True)
            raise

    async def _handle_realtime_data(self, data):
        """Handle real-time data updates from WebSocket."""
        try:
            _LOGGER.debug("=== WebSocket Data Received ===")
            _LOGGER.debug("Raw WebSocket data: %s", data)
            _LOGGER.debug("Data type: %s", type(data))

            # Handle current_water_flow_gpm specifically
            if isinstance(data, dict) and data.get("name") == "current_water_flow_gpm":
                _LOGGER.debug("Processing water flow data...")
                if (
                    "converted_property" in data
                    and "value" in data["converted_property"]
                ):
                    corrected_flow_value = data["converted_property"]["value"]
                    original_value = data.get("value", "unknown")

                    _LOGGER.info(
                        "🌊 WebSocket water flow update: raw=%s -> converted=%s gpm",
                        original_value,
                        corrected_flow_value,
                    )

                    # Store the corrected value and update the library
                    self._realtime_data["current_water_flow_gpm"] = corrected_flow_value
                    self._realtime_data_timestamps["current_water_flow_gpm"] = (
                        time.time()
                    )
                    
                    _LOGGER.debug("Stored realtime data: %s", self._realtime_data)

                    # Update the library's external real-time data
                    await self.hass.async_add_executor_job(
                        self._iqua_softener.update_external_realtime_data,
                        {data["name"]: data},
                    )
                    
                    _LOGGER.debug("Updated library external realtime data")
                else:
                    _LOGGER.warning("Water flow data missing converted_property or value: %s", data)
            else:
                _LOGGER.debug("Non-water-flow data received: name=%s", data.get("name", "unknown"))

            # Trigger coordinator update to refresh all entities
            _LOGGER.debug("Triggering coordinator refresh...")
            await self.async_request_refresh()
            _LOGGER.debug("✓ Real-time data processed and coordinator refreshed")
            _LOGGER.debug("=== End WebSocket Data Processing ===")
        except Exception as err:
            _LOGGER.error("❌ Failed to handle real-time data: %s", err, exc_info=True)

    async def async_retry_websocket(self):
        """Manually retry WebSocket connection."""
        _LOGGER.info("Manual WebSocket retry requested")
        self._websocket_failed_permanently = False
        await self.async_stop_websocket()
        await self.async_start_websocket()

    async def _async_update_data(self) -> IquaSoftenerData:
        _LOGGER.debug("Fetching data from iQua API")
        
        # Start WebSocket after first successful data fetch (post-bootstrap)
        if (self._enable_websocket and 
            not self._websocket_start_delayed and 
            not self._websocket_task and 
            not self._websocket_failed_permanently):
            _LOGGER.info("🚀 Starting WebSocket after bootstrap completion...")
            self._websocket_start_delayed = True
            # Schedule WebSocket start as a background task to avoid blocking data fetch
            self.hass.async_create_task(self.async_start_websocket())
        
        try:
            data = await self.hass.async_add_executor_job(
                lambda: self._iqua_softener.get_data()
            )
            _LOGGER.debug("Successfully fetched data from iQua API")
            return data
        except TypeError as err:
            # Handle library authentication issues
            if "'str' object is not callable" in str(err):
                _LOGGER.error(
                    "iQua library authentication error - may indicate library version issue: %s",
                    err,
                )
                # Try to recreate the iqua client to reset authentication state
                try:
                    _LOGGER.debug(
                        "Attempting to recreate iQua client to reset authentication"
                    )
                    from iqua_softener import IquaSoftener

                    self._iqua_softener = IquaSoftener(
                        self._username,
                        self._password,
                        self._device_serial_number,
                    )
                    # Try the request again with fresh client
                    data = await self.hass.async_add_executor_job(
                        lambda: self._iqua_softener.get_data()
                    )
                    _LOGGER.info("Successfully recovered from authentication error")

                    # Also reset WebSocket authentication if enabled
                    if (
                        self._enable_websocket
                        and not self._websocket_failed_permanently
                    ):
                        _LOGGER.debug(
                            "Resetting WebSocket authentication after main auth recovery"
                        )
                        self.hass.async_create_task(self.async_reset_websocket_authentication())

                    return data
                except Exception as recovery_err:
                    _LOGGER.error(
                        "Failed to recover from authentication error: %s", recovery_err
                    )
                    raise UpdateFailed(f"iQua library authentication error: {err}")
            else:
                _LOGGER.error("Unexpected TypeError in iQua API call: %s", err)
                raise UpdateFailed(f"Unexpected error: {err}")
        except IquaSoftenerException as err:
            _LOGGER.error("Get data failed: %s", err)
            raise UpdateFailed(f"Get data failed: {err}")
        except Exception as err:
            _LOGGER.error("Unexpected error fetching data: %s", err)
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
        # Enhanced debug logging to diagnose WebSocket flow data issues
        _LOGGER.debug("=== Water Flow Sensor Update Debug ===")
        _LOGGER.debug("API current_water_flow value: %s", data.current_water_flow)
        
        # Use the library's get_realtime_property method for real-time flow data
        realtime_flow = self.coordinator._iqua_softener.get_realtime_property(
            "current_water_flow_gpm"
        )
        
        _LOGGER.debug("Library realtime flow value: %s", realtime_flow)
        _LOGGER.debug("Coordinator realtime_data: %s", getattr(self.coordinator, '_realtime_data', {}))
        _LOGGER.debug("WebSocket task status: %s", 
                     "running" if getattr(self.coordinator, '_websocket_task', None) and not self.coordinator._websocket_task.done() else "not running")

        if realtime_flow is not None:
            # Use real-time WebSocket data
            self._attr_native_value = realtime_flow
            _LOGGER.debug("✓ Using real-time water flow from library: %s", realtime_flow)
        else:
            # Fall back to regular API data
            self._attr_native_value = data.current_water_flow
            _LOGGER.debug("⚠ Using API water flow (no realtime): %s", self._attr_native_value)

        self._attr_native_unit_of_measurement = (
            VOLUME_FLOW_RATE_LITERS_PER_MINUTE
            if data.volume_unit == IquaSoftenerVolumeUnit.LITERS
            else VOLUME_FLOW_RATE_GALLONS_PER_MINUTE
        )
        
        _LOGGER.debug("Final flow value: %s %s", self._attr_native_value, self._attr_native_unit_of_measurement)
        _LOGGER.debug("=== End Water Flow Sensor Update ===")


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
