"""Config flow for Laifen integration."""
from __future__ import annotations

import logging
import voluptuous as vol
from typing import Any

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.data_entry_flow import FlowResult
from .const import CONF_LOW_TRAFFIC_BATTERY, DOMAIN

_LOGGER = logging.getLogger(__name__)

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Laifen."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if not bluetooth.async_scanner_count(self.hass, connectable=False):
            return self.async_abort(reason="bluetooth_not_available")

        # async_discovered_service_info only reads from a cache — it does not
        # trigger a scan. The Laifen brush only advertises while awake, so it
        # may never have been captured into that cache. Request an on-demand
        # active scan first so a currently-advertising device shows up
        # immediately, instead of waiting for the next periodic scan cycle.
        # This API is only available in recent HA Core versions, so fall back
        # gracefully (old cache-only behaviour) if it doesn't exist.
        active_scan = getattr(bluetooth, "async_request_active_scan", None)
        if active_scan is not None:
            try:
                await active_scan(self.hass)
            except Exception as e:
                _LOGGER.debug(f"async_request_active_scan failed: {e}")

        # Discover available Bluetooth devices
        devices = bluetooth.async_discovered_service_info(self.hass)

        _LOGGER.debug(f"Discovered devices: {[(d.name, d.address, d.service_uuids) for d in devices]}")

        LAIFEN_SERVICE_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"

        found_devices = {}
        for device in devices:
            name_match = device.name and device.name.startswith("LFTB")
            uuid_match = LAIFEN_SERVICE_UUID in (device.service_uuids or [])
            if name_match or uuid_match:
                label = device.name or device.address
                found_devices[label] = device.address

        if not found_devices:
            _LOGGER.debug("No Laifen devices found via Bluetooth scan.")
            return self.async_abort(reason="no_matching_device_found")

        # If multiple devices, prompt user to select
        if len(found_devices) > 1:
            return self.async_show_form(
                step_id="select_device",
                data_schema=vol.Schema({
                    vol.Required("mac_address"): vol.In(found_devices.values())
                })
            )

        # Auto-create entry if only one device found
        device_name, device_address = next(iter(found_devices.items()))
        return self.async_create_entry(title="Laifen Toothbrush", data={"devices": [device_address]})

    
    async def async_step_select_device(self, user_input: dict[str, Any]) -> FlowResult:
        """Handle user-selected device registration."""
        selected_address = user_input["mac_address"]
        return self.async_create_entry(title=f"Laifen Toothbrush ({selected_address})", data={"devices": [selected_address]})

    @staticmethod
    def async_get_options_flow(config_entry):
        """Per-entry options. The checkbox defaults to off."""
        flow = LaifenOptionsFlow()
        flow._entry = config_entry
        return flow


class LaifenOptionsFlow(config_entries.OptionsFlow):
    """Enable low-traffic battery monitoring on this entry only.

    ``OptionsFlow`` is constructed with no arguments so newer Home Assistant
    releases, which set ``config_entry`` themselves, and older releases,
    which pass the entry into ``async_get_options_flow``, both work.
    """

    def _current_entry(self):
        entry = getattr(self, "_entry", None)
        if entry is not None:
            return entry
        return self.config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        entry = self._current_entry()
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = bool(entry.options.get(CONF_LOW_TRAFFIC_BATTERY, False))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOW_TRAFFIC_BATTERY, default=current): bool,
                }
            ),
        )
