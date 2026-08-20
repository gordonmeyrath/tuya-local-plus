"""Automatic synchronization of directly addressable Smart Life devices."""

import asyncio
import logging
from datetime import timedelta
from ipaddress import IPv4Address, ip_address, ip_network
from time import monotonic

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started

from .cloud import Cloud, async_load_auth
from .const import (
    CLOUD_ACCOUNT_TYPE,
    CLOUD_SYNC_SOURCE,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    DATA_CLOUD_SYNC,
    DOMAIN,
)
from .discovery import async_force_scan_devices, async_scan_devices
from .helpers.config import get_device_id

_LOGGER = logging.getLogger(__name__)
CLOUD_SYNC_INTERVAL = timedelta(minutes=5)
FORCE_SCAN_INTERVAL = timedelta(minutes=30)
AUTO_IMPORT_TIMEOUT = 120


def _configured_device_ids(hass: HomeAssistant) -> set[str]:
    """Return physical device identities from every Tuya Local config entry."""
    return {
        get_device_id(entry.data)
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_TYPE) != CLOUD_ACCOUNT_TYPE
        and entry.data.get(CONF_DEVICE_ID)
    }


def _is_direct_local_device(device: dict) -> bool:
    """Return whether cloud metadata represents a local Wi-Fi device."""
    return bool(device.get(CONF_DEVICE_ID) or device.get("id")) and not any(
        (
            device.get("is_hub"),
            device.get("sub"),
            device.get("node_id"),
            not device.get(CONF_LOCAL_KEY),
            not device.get("support_local", True),
        )
    )


class TuyaCloudSync:
    """Periodically import newly paired, supported local Tuya devices."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._unsub_interval = None
        self._unsub_start = None
        self._sync_task = None
        self._rejected: set[str] = set()
        self._last_force_scan = 0

    @callback
    def async_start(self) -> None:
        """Schedule periodic synchronization and run once immediately."""
        if self._unsub_interval is not None:
            return
        self._unsub_interval = async_track_time_interval(
            self._hass, self._async_sync, CLOUD_SYNC_INTERVAL
        )
        self._unsub_start = async_at_started(self._hass, self._async_start_initial_sync)

    @callback
    def _async_start_initial_sync(self, hass: HomeAssistant) -> None:
        """Run the initial import only after Home Assistant is available."""
        self._unsub_start = None
        self._sync_task = self._hass.async_create_task(
            self._async_sync(), "tuya_local cloud sync"
        )

    @callback
    def async_stop(self) -> None:
        """Stop future synchronization work."""
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        if self._unsub_start is not None:
            self._unsub_start()
            self._unsub_start = None
        if self._sync_task is not None and not self._sync_task.done():
            self._sync_task.cancel()
        self._sync_task = None

    async def _async_sync(self, now=None) -> None:
        """Fetch cloud metadata and import newly paired LAN devices."""
        if (
            self._sync_task is not None
            and self._sync_task is not asyncio.current_task()
        ):
            if not self._sync_task.done():
                return
        self._sync_task = asyncio.current_task()
        try:
            if not await async_load_auth(self._hass):
                _LOGGER.warning("Cloud sync is waiting for Smart Life authentication")
                return
            cloud = Cloud(self._hass)
            await cloud.async_initialize()
            cloud_devices = await cloud.async_get_devices()
            candidates = {
                device["id"]: device
                for device in cloud_devices.values()
                if _is_direct_local_device(device)
            }
            await self._async_refresh_keys(candidates)

            configured = _configured_device_ids(self._hass)
            pending = {
                device_id: device
                for device_id, device in candidates.items()
                if device_id not in configured and device_id not in self._rejected
            }
            if not pending:
                return

            discovered = await async_scan_devices(self._hass)
            discovered_ids = {
                info.get("gwId") or info.get("id") for info in discovered.values()
            }
            networks = self._local_networks()
            force_pending = [
                device
                for device_id, device in pending.items()
                if device_id not in discovered_ids
            ]
            if (
                force_pending
                and networks
                and not discovered
                and monotonic() - self._last_force_scan
                >= FORCE_SCAN_INTERVAL.total_seconds()
            ):
                self._last_force_scan = monotonic()
                try:
                    forced = await async_force_scan_devices(
                        self._hass, force_pending, networks
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _LOGGER.warning(
                        "TCP force scan failed; continuing with UDP discoveries: %s",
                        exc,
                    )
                else:
                    discovered.update(forced)
            lan_devices = {}
            for address, info in discovered.items():
                device_id = info.get("gwId") or info.get("id")
                if device_id:
                    lan_devices[device_id] = {
                        **info,
                        "ip": info.get("ip") or address,
                    }

            semaphore = asyncio.Semaphore(3)

            async def import_device(device_id, cloud_device):
                local = lan_devices.get(device_id)
                if not local or not local.get("ip"):
                    return
                try:
                    async with semaphore, asyncio.timeout(AUTO_IMPORT_TIMEOUT):
                        result = await self._hass.config_entries.flow.async_init(
                            DOMAIN,
                            context={"source": CLOUD_SYNC_SOURCE},
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
                            },
                        )
                except TimeoutError:
                    _LOGGER.warning(
                        "%s: automatic import timed out; will retry later",
                        cloud_device.get("name") or device_id,
                    )
                    return
                except Exception:
                    _LOGGER.exception(
                        "%s: automatic import failed; will retry later",
                        cloud_device.get("name") or device_id,
                    )
                    return
                if result.get("reason") == "bulk_not_supported":
                    self._rejected.add(device_id)
                elif result.get("type") == "create_entry":
                    _LOGGER.warning(
                        "Automatically imported new Smart Life device: %s",
                        cloud_device.get("name") or device_id,
                    )

            await asyncio.gather(
                *(
                    import_device(device_id, cloud_device)
                    for device_id, cloud_device in pending.items()
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Smart Life cloud synchronization failed")
        finally:
            if self._sync_task is asyncio.current_task():
                self._sync_task = None

    def _local_networks(self) -> list[str]:
        """Derive conservative /24 scan ranges from verified device hosts."""
        networks = set()
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            host = {**entry.data, **entry.options}.get(CONF_HOST)
            try:
                address = ip_address(host)
            except TypeError, ValueError:
                continue
            if (
                isinstance(address, IPv4Address)
                and address.is_private
                and not address.is_loopback
            ):
                networks.add(str(ip_network(f"{address}/24", strict=False)))
        return sorted(networks)

    async def _async_refresh_keys(self, cloud_devices: dict[str, dict]) -> None:
        """Update local keys after a device was re-paired in Smart Life."""
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_TYPE) == CLOUD_ACCOUNT_TYPE:
                continue
            device_id = entry.data.get(CONF_DEVICE_ID)
            cloud_device = cloud_devices.get(device_id)
            if not cloud_device:
                continue
            local_key = cloud_device.get(CONF_LOCAL_KEY)
            effective = {**entry.data, **entry.options}
            if not local_key or local_key == effective.get(CONF_LOCAL_KEY):
                continue
            options = entry.options
            if CONF_LOCAL_KEY in options:
                options = {**options, CONF_LOCAL_KEY: local_key}
            self._hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_LOCAL_KEY: local_key},
                options=options,
            )
            _LOGGER.warning("%s: updated local key from Smart Life", entry.title)


async def async_start_cloud_sync(hass: HomeAssistant) -> None:
    """Start the shared cloud synchronization service."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_CLOUD_SYNC) is not None:
        return
    cloud_sync = TuyaCloudSync(hass)
    domain_data[DATA_CLOUD_SYNC] = cloud_sync
    for source in (SOURCE_INTEGRATION_DISCOVERY, CLOUD_SYNC_SOURCE):
        stale_flows = hass.config_entries.flow.async_progress_by_handler(
            DOMAIN,
            include_uninitialized=True,
            match_context={"source": source},
        )
        for flow in stale_flows:
            try:
                hass.config_entries.flow.async_abort(flow["flow_id"])
            except UnknownFlow:
                pass
    cloud_sync.async_start()


@callback
def async_stop_cloud_sync(hass: HomeAssistant) -> None:
    """Stop the shared cloud synchronization service."""
    domain_data = hass.data.get(DOMAIN, {})
    cloud_sync = domain_data.pop(DATA_CLOUD_SYNC, None)
    if cloud_sync is not None:
        cloud_sync.async_stop()
