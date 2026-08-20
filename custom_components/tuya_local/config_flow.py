import asyncio
import logging
from collections import OrderedDict
from typing import Any

import tinytuya
import voluptuous as vol
from homeassistant.config_entries import (
    CONN_CLASS_LOCAL_PUSH,
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult, FlowResultType, UnknownFlow
from homeassistant.helpers.selector import (
    QrCodeSelector,
    QrCodeSelectorConfig,
    QrErrorCorrectionLevel,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import DOMAIN
from .cloud import Cloud, async_load_auth
from .const import (
    API_PROTOCOL_VERSIONS,
    CLOUD_ACCOUNT_TYPE,
    CLOUD_ACCOUNT_UNIQUE_ID,
    CLOUD_PENDING_TYPE,
    CLOUD_SYNC_SOURCE,
    CONF_DEVICE_CID,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    CONF_USER_CODE,
    DATA_CLOUD_IMPORTING,
    DATA_STORE,
)
from .device import TuyaLocalDevice
from .discovery import async_scan_devices
from .helpers.config import get_device_id
from .helpers.device_config import get_config
from .helpers.log import log_json

_LOGGER = logging.getLogger(__name__)
BULK_MATCH_MIN_QUALITY = 50
DATA_BULK_CANCELLED = "bulk_cancelled"
CATEGORY_TYPE_HINTS = {
    "cz": ("switch", "plug", "outlet", "socket", "relay", "powerstrip"),
    "dj": ("light",),
    "gyd": ("light", "spotlight"),
    "kg": ("switch", "plug", "outlet", "socket", "relay"),
    "kqzg": ("air_fryer", "airfryer"),
    "tdq": ("switch", "plug", "outlet", "socket", "powerstrip"),
}
DEVICE_DETAILS_URL = (
    "https://github.com/make-all/tuya-local/blob/main/DEVICE_DETAILS.md"
    "#finding-your-device-id-and-local-key"
)


class ConfigFlowHandler(ConfigFlow, domain=DOMAIN):
    VERSION = 13
    MINOR_VERSION = 22
    CONNECTION_CLASS = CONN_CLASS_LOCAL_PUSH
    device = None
    data = {}

    __qr_code: str | None = None
    __cloud_devices: dict[str, Any] = {}
    __discovered_device: dict[str, Any] | None = None
    __bulk_mode = False

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.cloud = None
        self.__bulk_task = None
        self.__bulk_result = None
        self.__bulk_total = 0

    async def init_cloud(self):
        await async_load_auth(self.hass)
        if self.cloud is None:
            self.cloud = Cloud(self.hass)

    async def async_step_integration_discovery(self, discovery_info):
        """Handle a device found on the LAN by the background scanner.

        Pre-fills the manual setup form with the discovered id/ip/version; the
        user still supplies the local key. Aborts if the device is already
        configured or has been ignored.
        """
        device_id = discovery_info.get(CONF_DEVICE_ID)
        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()
        # Reuse the cloud-device plumbing that async_step_local reads for its
        # form defaults; the local key is not known from discovery.
        self.__discovered_device = {
            "id": device_id,
            "ip": discovery_info.get(CONF_HOST),
            "version": discovery_info.get("version"),
            "local_product_id": discovery_info.get("product_id"),
            CONF_LOCAL_KEY: "",
        }
        self.context["title_placeholders"] = {
            "name": discovery_info.get(CONF_HOST) or device_id
        }
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        errors = {}

        if self.hass.data.get(DOMAIN) is None:
            self.hass.data[DOMAIN] = {}
        if self.hass.data[DOMAIN].get(DATA_STORE) is None:
            self.hass.data[DOMAIN][DATA_STORE] = {}

        if user_input is not None:
            mode = user_input.get("setup_mode")
            if mode in ("cloud", "cloud_bulk", "cloud_fresh_login"):
                self.__bulk_mode = mode == "cloud_bulk"
                await self.init_cloud()
                try:
                    if mode == "cloud_fresh_login":
                        # Force a fresh login
                        await self.cloud.async_logout()

                    if self.cloud.is_authenticated:
                        self.__cloud_devices = await self.cloud.async_get_devices()
                        if self.__bulk_mode:
                            return await self.async_step_bulk_import()
                        return await self.async_step_choose_device()
                except Exception as e:
                    # Re-authentication is needed.
                    _LOGGER.warning("Connection test failed with %s %s", type(e), e)
                    _LOGGER.warning("Re-authentication is required.")
                return await self.async_step_cloud()
            if mode == "manual":
                return await self.async_step_local()

        # Build form
        fields: OrderedDict[vol.Marker, Any] = OrderedDict()
        fields[vol.Required("setup_mode")] = SelectSelector(
            SelectSelectorConfig(
                options=["cloud", "cloud_bulk", "manual", "cloud_fresh_login"],
                mode=SelectSelectorMode.LIST,
                translation_key="setup_mode",
            )
        )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(fields),
            errors=errors or {},
            last_step=False,
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step user."""
        errors = {}
        placeholders = {}
        await self.init_cloud()

        if user_input is not None:
            response = await self.cloud.async_get_qr_code(user_input[CONF_USER_CODE])
            if response:
                self.__qr_code = response
                return await self.async_step_scan()

            errors["base"] = "login_error"
            placeholders = self.cloud.last_error

        else:
            user_input = {}

        return self.async_show_form(
            step_id="cloud",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USER_CODE, default=user_input.get(CONF_USER_CODE, "")
                    ): str,
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step scan."""
        if user_input is None:
            return self.async_show_form(
                step_id="scan",
                data_schema=vol.Schema(
                    {
                        vol.Optional("QR"): QrCodeSelector(
                            config=QrCodeSelectorConfig(
                                data=f"tuyaSmart--qrLogin?token={self.__qr_code}",
                                scale=5,
                                error_correction_level=QrErrorCorrectionLevel.QUARTILE,
                            )
                        )
                    }
                ),
            )
        await self.init_cloud()
        if not await self.cloud.async_login():
            # Try to get a new QR code on failure
            response = await self.cloud.async_get_qr_code()
            errors = {"base": "login_error"}
            placeholders = self.cloud.last_error
            if response:
                self.__qr_code = response

            return self.async_show_form(
                step_id="scan",
                errors=errors,
                data_schema=vol.Schema(
                    {
                        vol.Optional("QR"): QrCodeSelector(
                            config=QrCodeSelectorConfig(
                                data=f"tuyaSmart--qrLogin?token={self.__qr_code}",
                                scale=5,
                                error_correction_level=QrErrorCorrectionLevel.QUARTILE,
                            )
                        )
                    }
                ),
                description_placeholders=placeholders,
            )

        self.__cloud_devices = await self.cloud.async_get_devices()
        if self.__discovered_device:
            # If local discovery already found a device, we can skip the choose device step
            # after updating discovery_info.
            device_choice = self.__cloud_devices.get(self.__discovered_device["id"])
            if device_choice:
                self.__discovered_device[CONF_LOCAL_KEY] = device_choice.get(
                    CONF_LOCAL_KEY
                )
                self.__discovered_device["product_id"] = device_choice.get("product_id")
                self.__discovered_device["product_name"] = device_choice.get(
                    "product_name"
                )
            return await self.async_step_local()
        if self.__bulk_mode:
            return await self.async_step_bulk_import()
        return await self.async_step_choose_device()

    def _bulk_candidates(self) -> list[SelectOptionDict]:
        """Return cloud devices that can be imported directly over the LAN."""
        configured = {
            get_device_id(entry.data)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(CONF_TYPE) != CLOUD_ACCOUNT_TYPE
            and entry.data.get(CONF_DEVICE_ID)
        }
        candidates = []
        for key, device in self.__cloud_devices.items():
            if (
                device.get("exists")
                or device.get("is_hub")
                or device.get("sub")
                or device.get("node_id")
                or device.get("id") in configured
                or not device.get(CONF_LOCAL_KEY)
                or not device.get("support_local", True)
            ):
                continue
            status = "" if device.get("online") else " OFFLINE"
            candidates.append(
                SelectOptionDict(
                    value=key,
                    label=(
                        f"{device.get('name') or device.get('id')} "
                        f"({device.get('product_name') or 'Unknown'}){status}"
                    ),
                )
            )
        return candidates

    async def async_step_bulk_import(self, user_input=None):
        """Import multiple directly addressable devices from Tuya cloud."""
        candidates = self._bulk_candidates()
        if not candidates:
            self.__bulk_result = {"imported": 0, "skipped": 0, "failed": 0}
            return await self.async_step_bulk_complete()

        if user_input is None:
            selector = SelectSelector(
                SelectSelectorConfig(
                    options=candidates,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
            return self.async_show_form(
                step_id="bulk_import",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            "device_ids",
                            default=[candidate["value"] for candidate in candidates],
                        ): vol.All(selector, vol.Length(min=1)),
                    }
                ),
            )

        selected = list(dict.fromkeys(user_input["device_ids"]))
        self.hass.data.setdefault(DOMAIN, {}).setdefault(
            DATA_BULK_CANCELLED, set()
        ).discard(self.flow_id)
        self.hass.data[DOMAIN].setdefault(DATA_CLOUD_IMPORTING, set()).add(self.flow_id)
        self.__bulk_total = len(selected)
        self.__bulk_task = self.hass.async_create_task(
            self._async_bulk_import(selected)
        )
        return self.async_show_progress(
            step_id="bulk_progress",
            progress_action="bulk_import",
            progress_task=self.__bulk_task,
        )

    async def async_step_bulk_progress(self, user_input=None):
        """Show progress while selected devices are imported."""
        if not self.__bulk_task.done():
            return self.async_show_progress(
                step_id="bulk_progress",
                progress_action="bulk_import",
                progress_task=self.__bulk_task,
            )
        try:
            self.__bulk_result = self.__bulk_task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep the config flow usable after scan failures
            _LOGGER.exception("Bulk import task failed: %s", exc)
            self.__bulk_result = {
                "imported": 0,
                "skipped": 0,
                "failed": self.__bulk_total,
            }
        finally:
            self.hass.data.setdefault(DOMAIN, {}).setdefault(
                DATA_CLOUD_IMPORTING, set()
            ).discard(self.flow_id)
        return self.async_show_progress_done(next_step_id="bulk_complete")

    async def async_step_bulk_complete(self, user_input=None):
        """Create the persistent cloud-sync entry after initial bulk import."""
        result = self.__bulk_result or {"imported": 0, "skipped": 0, "failed": 0}
        await self.async_set_unique_id(CLOUD_ACCOUNT_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Smart Life cloud sync",
            data={
                CONF_TYPE: CLOUD_ACCOUNT_TYPE,
                "initial_import": result,
            },
        )

    async def _async_bulk_import(self, selected):
        """Discover once and import selected devices with bounded concurrency."""
        discovered = await async_scan_devices(self.hass)
        lan_devices = {}
        for address, info in discovered.items():
            device_id = info.get("gwId") or info.get("id")
            if device_id:
                lan_devices[device_id] = {**info, "ip": info.get("ip") or address}

        semaphore = asyncio.Semaphore(3)

        async def import_one(key):
            cloud_device = self.__cloud_devices[key]
            device_id = cloud_device["id"]
            local = lan_devices.get(device_id)
            if not local or not local.get("ip"):
                return "failed"
            try:
                async with semaphore, asyncio.timeout(120):
                    result = await self.hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={
                            "source": CLOUD_SYNC_SOURCE,
                            "bulk_parent": self.flow_id,
                        },
                        data={
                            CONF_DEVICE_ID: device_id,
                            CONF_HOST: local["ip"],
                            CONF_LOCAL_KEY: cloud_device[CONF_LOCAL_KEY],
                            CONF_PROTOCOL_VERSION: local.get("version") or "auto",
                            CONF_POLL_ONLY: False,
                            CONF_NAME: cloud_device.get("name")
                            or cloud_device.get("product_name"),
                            "product_ids": list(
                                dict.fromkeys(
                                    product_id
                                    for product_id in (
                                        cloud_device.get("product_id"),
                                        local.get("productKey"),
                                    )
                                    if product_id
                                )
                            ),
                            "category": cloud_device.get("category"),
                            "bulk_parent": self.flow_id,
                        },
                    )
            except Exception as exc:  # isolate one device from the whole import
                _LOGGER.warning("Bulk import failed for %s: %s", device_id, exc)
                return "failed"
            if result["type"] == FlowResultType.CREATE_ENTRY:
                return "imported"
            elif result.get("reason") == "pending_completed":
                return "imported"
            elif result.get("reason") in (
                "already_configured",
                "already_in_progress",
            ):
                return "skipped"
            return "failed"

        counts = {"imported": 0, "skipped": 0, "failed": 0}
        tasks = [self.hass.async_create_task(import_one(key)) for key in selected]
        try:
            for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
                counts[await task] += 1
                self.async_update_progress(completed / len(tasks))
            return counts
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def async_step_cloud_inventory(self, inventory_data):
        """Create an unavailable inventory entry until local setup is possible."""
        inventory_id = inventory_data["cloud_inventory_id"]
        await self.async_set_unique_id(inventory_id)
        self._abort_if_unique_id_configured()
        title = (
            inventory_data.get(CONF_NAME)
            or inventory_data.get("product_name")
            or "Tuya cloud device"
        )
        data = {
            CONF_TYPE: CLOUD_PENDING_TYPE,
            "cloud_inventory_id": inventory_id,
            CONF_DEVICE_ID: inventory_data[CONF_DEVICE_ID],
            CONF_LOCAL_KEY: inventory_data.get(CONF_LOCAL_KEY, ""),
            "product_id": inventory_data.get("product_id"),
            "product_name": inventory_data.get("product_name"),
            "category": inventory_data.get("category"),
            "cloud_online": inventory_data.get("cloud_online", False),
            "pending_reason": inventory_data.get("pending_reason", "awaiting_lan"),
        }
        if inventory_data.get(CONF_DEVICE_CID):
            data[CONF_DEVICE_CID] = inventory_data[CONF_DEVICE_CID]
        return self.async_create_entry(title=title, data=data)

    async def async_step_import(self, import_data):
        """Validate and automatically create one cloud bulk-import entry."""
        bulk_parent = import_data.get("bulk_parent")
        cancelled = self.hass.data.setdefault(DOMAIN, {}).setdefault(
            DATA_BULK_CANCELLED, set()
        )
        if bulk_parent in cancelled:
            return self.async_abort(reason="bulk_cancelled")
        device_id = import_data[CONF_DEVICE_ID]
        entries = self.hass.config_entries.async_entries(DOMAIN)
        pending_entry = next(
            (
                entry
                for entry in entries
                if entry.data.get(CONF_TYPE) == CLOUD_PENDING_TYPE
                and (entry.data.get("cloud_inventory_id") or entry.unique_id)
                == device_id
            ),
            None,
        )
        if any(
            entry.data.get(CONF_TYPE) not in (CLOUD_ACCOUNT_TYPE, CLOUD_PENDING_TYPE)
            and get_device_id(entry.data) == device_id
            for entry in entries
            if entry.data.get(CONF_DEVICE_ID)
        ):
            return self.async_abort(reason="already_configured")
        if pending_entry is None:
            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()

        protocol = import_data.get(CONF_PROTOCOL_VERSION, "auto")
        if protocol != "auto":
            protocol = float(protocol)
        config = {
            CONF_DEVICE_ID: device_id,
            CONF_HOST: import_data[CONF_HOST],
            CONF_LOCAL_KEY: import_data[CONF_LOCAL_KEY],
            CONF_PROTOCOL_VERSION: protocol,
            CONF_POLL_ONLY: import_data.get(CONF_POLL_ONLY, False),
        }
        device = await async_test_connection(config, self.hass)
        if device is None:
            return self.async_abort(reason="bulk_connection_failed")

        product_ids = import_data.get("product_ids") or [import_data.get("product_id")]
        for product_id in product_ids:
            if not product_id:
                continue
            device.set_detected_product_id(product_id)

        best_type = None
        best_quality = 0
        best_manufacturer = None
        best_model = None
        best_types = []
        matches = []
        for device_type in await device.async_possible_types():
            quality = device_type.match_quality(
                device._get_cached_state(), device._product_ids
            )
            matches.append((device_type, quality))
            if quality < best_quality:
                continue
            if quality == best_quality and best_type is not None:
                if device_type.config_type not in {
                    match.config_type for match in best_types
                }:
                    best_types.append(device_type)
                continue
            best_type = device_type
            best_types = [device_type]
            best_quality = quality
            product_entries = device_type.product_display_entries(device._product_ids)
            best_manufacturer, best_model = next(iter(product_entries), (None, None))

        ambiguous = len(best_types) > 1
        if best_type is None or best_quality < BULK_MATCH_MIN_QUALITY:
            _LOGGER.warning(
                "%s: automatic import skipped; best type=%s quality=%s ambiguous=%s category=%s",
                import_data.get(CONF_NAME) or device_id,
                best_type.config_type if best_type else "none",
                best_quality,
                ambiguous,
                import_data.get("category"),
            )
            return self.async_abort(reason="bulk_not_supported")
        category = import_data.get("category")
        hints = CATEGORY_TYPE_HINTS.get(category)
        if hints and not any(hint in best_type.config_type for hint in hints):
            rejected_type = best_type.config_type
            compatible = [
                (match, quality)
                for match, quality in matches
                if any(hint in match.config_type for hint in hints)
            ]
            compatible_quality = max((quality for _, quality in compatible), default=0)
            if compatible_quality < BULK_MATCH_MIN_QUALITY:
                _LOGGER.warning(
                    "%s: rejecting device type %s for Tuya category %s",
                    device_id,
                    best_type.config_type,
                    category,
                )
                return self.async_abort(reason="bulk_not_supported")
            best_types = [
                match for match, quality in compatible if quality == compatible_quality
            ]
            best_type = best_types[0]
            best_quality = compatible_quality
            ambiguous = len(best_types) > 1
            product_entries = best_type.product_display_entries(device._product_ids)
            best_manufacturer, best_model = next(iter(product_entries), (None, None))
            _LOGGER.warning(
                "%s: selected category-compatible type %s instead of %s",
                import_data.get(CONF_NAME) or device_id,
                best_type.config_type,
                rejected_type,
            )
        if ambiguous and (
            not hints
            or not all(
                any(hint in match.config_type for hint in hints) for match in best_types
            )
        ):
            _LOGGER.warning(
                "%s: automatic import skipped; incompatible tied profiles: %s",
                import_data.get(CONF_NAME) or device_id,
                ", ".join(match.config_type for match in best_types),
            )
            return self.async_abort(reason="bulk_not_supported")
        if ambiguous:
            _LOGGER.warning(
                "%s: selecting category-compatible profile %s from tied matches",
                import_data.get(CONF_NAME) or device_id,
                best_type.config_type,
            )
        if best_quality < 100 and not hints:
            _LOGGER.warning(
                "%s: automatic import skipped; non-exact type %s has no category validation",
                import_data.get(CONF_NAME) or device_id,
                best_type.config_type,
            )
            return self.async_abort(reason="bulk_not_supported")

        config[CONF_TYPE] = best_type.config_type
        if best_manufacturer:
            config[CONF_MANUFACTURER] = best_manufacturer
        if best_model:
            config[CONF_MODEL] = best_model
        if protocol == "auto" and device._protocol_configured != "auto":
            config[CONF_PROTOCOL_VERSION] = device._protocol_configured

        title = import_data.get(CONF_NAME)
        if not title:
            matched_config = await self.hass.async_add_executor_job(
                get_config, best_type.config_type
            )
            title = matched_config.name
        if bulk_parent in cancelled:
            return self.async_abort(reason="bulk_cancelled")
        if pending_entry is not None:
            self.hass.config_entries.async_update_entry(
                pending_entry,
                title=title,
                data=config,
                options={},
                unique_id=device_id,
            )
            return self.async_abort(reason="pending_completed")
        return self.async_create_entry(title=title, data=config)

    async def async_step_cloud_sync(self, import_data):
        """Import one device discovered by bulk or periodic cloud sync."""
        return await self.async_step_import(import_data)

    @callback
    def async_remove(self) -> None:
        """Cancel child imports when the parent bulk flow is removed."""
        if self.__bulk_task is None or self.__bulk_task.done():
            return
        self.hass.data.setdefault(DOMAIN, {}).setdefault(
            DATA_BULK_CANCELLED, set()
        ).add(self.flow_id)
        self.hass.data[DOMAIN].setdefault(DATA_CLOUD_IMPORTING, set()).discard(
            self.flow_id
        )
        if not self.__bulk_task.done():
            self.__bulk_task.cancel()
        child_flows = self.hass.config_entries.flow.async_progress_by_handler(
            DOMAIN,
            include_uninitialized=True,
            match_context={"bulk_parent": self.flow_id},
        )
        for child in child_flows:
            try:
                self.hass.config_entries.flow.async_abort(child["flow_id"])
            except UnknownFlow:
                pass

    async def async_step_choose_device(self, user_input=None):
        errors = {}
        if user_input is not None:
            device_choice = self.__cloud_devices[user_input["device_id"]]

            if device_choice["ip"] != "":
                # This is a directly addable device.
                if user_input["hub_id"] == "None":
                    device_choice["ip"] = ""
                    self.__discovered_device = device_choice
                    return await self.async_step_search()
                else:
                    # Show error if user selected a hub.
                    errors["base"] = "does_not_need_hub"
                    # Fall through to reshow the form.
            else:
                # This is an indirectly addressable device. Need to know which hub it is connected to.
                if user_input["hub_id"] != "None":
                    hub_choice = self.__cloud_devices[user_input["hub_id"]]
                    # Populate node_id or uuid and local_key from the child
                    # device to pass on complete information to the local step.
                    hub_choice["ip"] = ""
                    hub_choice[CONF_DEVICE_CID] = (
                        device_choice["node_id"] or device_choice["uuid"]
                    )
                    if device_choice.get(CONF_LOCAL_KEY):
                        hub_choice[CONF_LOCAL_KEY] = device_choice[CONF_LOCAL_KEY]
                    # Communicate the sub device product id to help match the
                    # correect device config in the next step.
                    hub_choice["product_id"] = device_choice["product_id"]
                    self.__discovered_device = hub_choice
                    return await self.async_step_search()
                else:
                    # Show error if user did not select a hub.
                    errors["base"] = "needs_hub"
                    # Fall through to reshow the form.

        device_list = []
        for key in self.__cloud_devices.keys():
            device_entry = self.__cloud_devices[key]
            if device_entry.get("exists"):
                continue
            if device_entry[CONF_LOCAL_KEY] != "":
                if device_entry["online"]:
                    device_list.append(
                        SelectOptionDict(
                            value=key,
                            label=f"{device_entry['name']} ({device_entry['product_name']})",
                        )
                    )
                else:
                    device_list.append(
                        SelectOptionDict(
                            value=key,
                            label=f"{device_entry['name']} ({device_entry['product_name']}) OFFLINE",
                        )
                    )

        _LOGGER.debug(f"Device count: {len(device_list)}")
        if len(device_list) == 0:
            return self.async_abort(reason="no_devices")

        device_selector = SelectSelector(
            SelectSelectorConfig(options=device_list, mode=SelectSelectorMode.DROPDOWN)
        )

        hub_list = []
        hub_list.append(SelectOptionDict(value="None", label="None"))
        for key in self.__cloud_devices.keys():
            hub_entry = self.__cloud_devices[key]
            if hub_entry["is_hub"]:
                hub_list.append(
                    SelectOptionDict(
                        value=key,
                        label=f"{hub_entry['name']} ({hub_entry['product_name']})",
                    )
                )

        _LOGGER.debug(f"Hub count: {len(hub_list) - 1}")

        hub_selector = SelectSelector(
            SelectSelectorConfig(options=hub_list, mode=SelectSelectorMode.DROPDOWN)
        )

        # Build form
        fields: OrderedDict[vol.Marker, Any] = OrderedDict()
        fields[vol.Required("device_id")] = device_selector
        fields[vol.Required("hub_id")] = hub_selector

        return self.async_show_form(
            step_id="choose_device",
            data_schema=vol.Schema(fields),
            errors=errors or {},
            last_step=False,
        )

    @property
    def _device_name_placeholder(self) -> str:
        """Return device name placeholder for step descriptions."""
        if self.__discovered_device and self.__discovered_device.get("product_name"):
            parts = []
            if self.__discovered_device.get("name"):
                parts.append(self.__discovered_device["name"])
            parts.append(self.__discovered_device["product_name"])
            return "**" + " — ".join(parts) + "**\n\n"
        return ""

    async def async_step_search(self, user_input=None):
        if user_input is not None:
            # Current IP is the WAN IP which is of no use. Need to try and discover to the local IP.
            # This scan will take 18s with the default settings. If we cannot find the device we
            # will just leave the IP address blank and hope the user can discover the IP by other
            # means such as router device IP assignments.
            _LOGGER.debug(
                f"Scanning network to get IP address for {self.__discovered_device.get('id', 'DEVICE_KEY_UNAVAILABLE')}."
            )
            self.__discovered_device["ip"] = ""
            try:
                local_device = await self.hass.async_add_executor_job(
                    scan_for_device, self.__discovered_device.get("id")
                )
            except OSError:
                local_device = {"ip": None, "version": ""}

            if local_device.get("ip"):
                _LOGGER.debug(f"Found: {local_device}")
                self.__discovered_device["ip"] = local_device.get("ip")
                self.__discovered_device["version"] = local_device.get("version")
                if not self.__discovered_device.get(CONF_DEVICE_CID):
                    self.__discovered_device["local_product_id"] = local_device.get(
                        "productKey"
                    )
            else:
                _LOGGER.warning(
                    f"Could not find device: {self.__discovered_device.get('id', 'DEVICE_KEY_UNAVAILABLE')}"
                )
            return await self.async_step_local()

        return self.async_show_form(
            step_id="search",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._device_name_placeholder,
            },
            errors={},
            last_step=False,
        )

    async def async_step_local(self, user_input=None):
        errors = {}
        devid_opts = {}
        host_opts = {"default": ""}
        key_opts = {}
        proto_opts = {"default": "auto"}
        polling_opts = {"default": False}
        devcid_opts = {}

        if self.__discovered_device is not None:
            # We already have some or all of the device settings from the cloud flow. Set them into the defaults.
            devid_opts = {"default": self.__discovered_device.get("id")}
            host_opts = {"default": self.__discovered_device.get("ip")}
            key_opts = {"default": self.__discovered_device.get(CONF_LOCAL_KEY)}
            if self.__discovered_device.get("version"):
                proto_opts = {"default": str(self.__discovered_device.get("version"))}
            if self.__discovered_device.get(CONF_DEVICE_CID):
                devcid_opts = {"default": self.__discovered_device.get(CONF_DEVICE_CID)}

        if user_input is not None:
            proto = user_input.get(CONF_PROTOCOL_VERSION)
            if proto != "auto":
                user_input[CONF_PROTOCOL_VERSION] = float(proto)
            self.device = await async_test_connection(user_input, self.hass)
            if self.device:
                self.data = user_input
                # If auto mode found a working protocol, save it so future
                # HA restarts connect directly without re-cycling all versions.
                self._auto_detected_protocol = None
                if (
                    user_input.get(CONF_PROTOCOL_VERSION) == "auto"
                    and self.device._protocol_configured != "auto"
                ):
                    self._auto_detected_protocol = self.device._protocol_configured
                    self.data = {
                        **self.data,
                        CONF_PROTOCOL_VERSION: self._auto_detected_protocol,
                    }
                if self.__discovered_device:
                    if self.__discovered_device.get("product_id"):
                        self.device.set_detected_product_id(
                            self.__discovered_device.get("product_id")
                        )
                    if self.__discovered_device.get("local_product_id"):
                        self.device.set_detected_product_id(
                            self.__discovered_device.get("local_product_id")
                        )
                await self.async_set_unique_id(get_device_id(user_input))
                self._abort_if_unique_id_configured()
                return await self.async_step_select_type()
            else:
                errors["base"] = "connection"
                devid_opts["default"] = user_input[CONF_DEVICE_ID]
                host_opts["default"] = user_input[CONF_HOST]
                key_opts["default"] = user_input[CONF_LOCAL_KEY]
                if CONF_DEVICE_CID in user_input:
                    devcid_opts["default"] = user_input[CONF_DEVICE_CID]
                proto_opts["default"] = str(user_input[CONF_PROTOCOL_VERSION])
                polling_opts["default"] = user_input[CONF_POLL_ONLY]

        return self.async_show_form(
            step_id="local",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID, **devid_opts): str,
                    vol.Required(CONF_HOST, **host_opts): str,
                    vol.Required(CONF_LOCAL_KEY, **key_opts): str,
                    vol.Required(
                        CONF_PROTOCOL_VERSION,
                        **proto_opts,
                    ): vol.In(["auto"] + [str(v) for v in API_PROTOCOL_VERSIONS]),
                    vol.Required(CONF_POLL_ONLY, **polling_opts): bool,
                    vol.Optional(CONF_DEVICE_CID, **devcid_opts): str,
                }
            ),
            description_placeholders={
                "device_details_url": DEVICE_DETAILS_URL,
                "device_name": self._device_name_placeholder,
            },
            errors=errors,
        )

    async def async_step_select_type(self, user_input=None):
        if user_input is not None:
            # Value is a compound key: "config_type||manufacturer||model"
            parts = user_input[CONF_TYPE].split("||", 2)
            self.data[CONF_TYPE] = parts[0]
            if len(parts) > 1 and parts[1]:
                self.data[CONF_MANUFACTURER] = parts[1]
            if len(parts) > 2 and parts[2]:
                self.data[CONF_MODEL] = parts[2]
            return await self.async_step_choose_entities()

        all_matches = []
        best_match = 0
        best_matching_type = None
        best_matching_key = None

        for dev_type in await self.device.async_possible_types():
            q = dev_type.match_quality(
                self.device._get_cached_state(),
                self.device._product_ids,
            )
            for manufacturer, model in dev_type.product_display_entries(
                self.device._product_ids
            ):
                key = f"{dev_type.config_type}||{manufacturer or ''}||{model or ''}"
                parts = [p for p in [manufacturer, model] if p]
                if parts:
                    label = f"{' '.join(parts)} ({dev_type.config_type})"
                else:
                    label = f"{dev_type.name} ({dev_type.config_type})"
                all_matches.append((SelectOptionDict(value=key, label=label), q))
                if q > best_match:
                    best_match = q
                    best_matching_type = dev_type.config_type
                    best_matching_key = key

        all_matches.sort(key=lambda x: x[1], reverse=True)
        type_options = [opt for opt, _ in all_matches]

        best_match = int(best_match)
        dps = self.device._get_cached_state()
        if self.__discovered_device:
            _LOGGER.warning(
                "Adding %s device with product id %s",
                self.__discovered_device.get("product_name", "UNKNOWN"),
                self.__discovered_device.get("product_id", "UNKNOWN"),
            )
            if self.__discovered_device.get(
                "local_product_id"
            ) and self.__discovered_device.get(
                "local_product_id"
            ) != self.__discovered_device.get("product_id"):
                _LOGGER.warning(
                    "Local product id differs from cloud: %s",
                    self.__discovered_device.get("local_product_id"),
                )
            try:
                await self.init_cloud()
                model = await self.cloud.async_get_datamodel(
                    self.__discovered_device.get("id"),
                )
                if model:
                    _LOGGER.warning(
                        "Partial cloud device spec:\n%s",
                        log_json(model),
                    )
            except Exception as e:
                _LOGGER.warning(
                    "Unable to fetch data model from cloud: %s %s",
                    type(e).__name__,
                    e,
                )
        _LOGGER.warning(
            "Device matches %s with quality of %d%%. LOCAL DPS: %s",
            best_matching_type,
            best_match,
            log_json(dps),
        )
        _LOGGER.warning(
            "Include the previous log messages with any new device request to https://github.com/make-all/tuya-local/issues/",
        )
        if type_options:
            detected = getattr(self, "_auto_detected_protocol", None)
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_TYPE,
                        default=best_matching_key,
                    ): SelectSelector(SelectSelectorConfig(options=type_options)),
                }
            )
            if detected:
                return self.async_show_form(
                    step_id="select_type_auto_detected",
                    data_schema=schema,
                    description_placeholders={
                        "detected_protocol": str(detected),
                        "device_name": self._device_name_placeholder,
                    },
                )
            return self.async_show_form(
                step_id="select_type",
                data_schema=schema,
                description_placeholders={
                    "device_name": self._device_name_placeholder,
                },
            )
        else:
            return self.async_abort(reason="not_supported")

    async def async_step_select_type_auto_detected(self, user_input=None):
        return await self.async_step_select_type(user_input)

    async def async_step_choose_entities(self, user_input=None):
        config = await self.hass.async_add_executor_job(
            get_config,
            self.data[CONF_TYPE],
        )
        if user_input is not None:
            title = user_input[CONF_NAME]
            del user_input[CONF_NAME]
            return self.async_create_entry(
                title=title, data={**self.data, **user_input}
            )
        default_name = config.name
        if self.__discovered_device and self.__discovered_device.get("name"):
            default_name = self.__discovered_device["name"]
        schema = {vol.Required(CONF_NAME, default=default_name): str}

        return self.async_show_form(
            step_id="choose_entities",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "device_name": self._device_name_placeholder,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    def __init__(self):
        """Initialize options flow."""
        pass

    async def async_step_init(self, user_input=None):
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        """Manage the options."""
        if self.config_entry.data.get(CONF_TYPE) in (
            CLOUD_ACCOUNT_TYPE,
            CLOUD_PENDING_TYPE,
        ):
            return self.async_abort(reason="cloud_sync_managed")
        errors = {}
        config = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            proto = user_input.get(CONF_PROTOCOL_VERSION)
            if proto != "auto":
                user_input[CONF_PROTOCOL_VERSION] = float(proto)
            config = {**config, **user_input}
            device = await async_test_connection(config, self.hass)
            if device:
                return self.async_create_entry(title="", data=user_input)
            else:
                errors["base"] = "connection"

        schema = {
            vol.Required(
                CONF_LOCAL_KEY,
                default=config.get(CONF_LOCAL_KEY, ""),
            ): str,
            vol.Required(CONF_HOST, default=config.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_PROTOCOL_VERSION,
                default=str(config.get(CONF_PROTOCOL_VERSION, "auto")),
            ): vol.In(["auto"] + [str(v) for v in API_PROTOCOL_VERSIONS]),
            vol.Required(
                CONF_POLL_ONLY, default=config.get(CONF_POLL_ONLY, False)
            ): bool,
        }
        cfg = await self.hass.async_add_executor_job(
            get_config,
            config[CONF_TYPE],
        )
        if cfg is None:
            return self.async_abort(reason="not_supported")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
            description_placeholders={"device_details_url": DEVICE_DETAILS_URL},
            errors=errors,
        )


def create_test_device(hass: HomeAssistant, config: dict):
    """Set up a tuya device based on passed in config."""
    subdevice_id = config.get(CONF_DEVICE_CID)
    device = TuyaLocalDevice(
        "Test",
        config[CONF_DEVICE_ID],
        config[CONF_HOST],
        config[CONF_LOCAL_KEY],
        config[CONF_PROTOCOL_VERSION],
        subdevice_id,
        hass,
        True,
    )

    return device


async def async_test_connection(config: dict, hass: HomeAssistant):
    domain_data = hass.data.get(DOMAIN)
    existing = domain_data.get(get_device_id(config)) if domain_data else None
    if existing and existing.get("device"):
        _LOGGER.info("Pausing existing device to test new connection parameters")
        existing["device"].pause()
        await asyncio.sleep(5)

    retval = None

    if config.get(CONF_PROTOCOL_VERSION) == "auto":
        # Test each protocol with a fresh device object. Reusing one device
        # object across protocol rotations causes 3.4/3.5 handshakes to fail:
        # the shared tinytuya object carries stale internal state from the
        # prior connection attempts.
        for proto in API_PROTOCOL_VERSIONS:
            proto_config = {**config, CONF_PROTOCOL_VERSION: proto}
            device = None
            try:
                device = await hass.async_add_executor_job(
                    create_test_device, hass, proto_config
                )
                await device.async_refresh()
                if device.has_returned_state:
                    retval = device
                    break
            except Exception as e:
                _LOGGER.debug("Protocol %s test failed with %s %s", proto, type(e), e)
            if device is not None:
                device._api.set_socketPersistent(False)
                if device._api.parent:
                    device._api.parent.set_socketPersistent(False)
    else:
        try:
            device = await hass.async_add_executor_job(
                create_test_device,
                hass,
                config,
            )
            await device.async_refresh()
            retval = device if device.has_returned_state else None
        except Exception as e:
            _LOGGER.warning("Connection test failed with %s %s", type(e), e)

    if existing and existing.get("device"):
        _LOGGER.info("Restarting device after test")
        existing["device"].resume()

    return retval


def scan_for_device(devid):
    return tinytuya.find_device(dev_id=devid)
