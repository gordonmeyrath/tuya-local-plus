"""
Active Tuya LAN discovery.

When a router hands out new DHCP leases (e.g. after a reboot) a Tuya device
can change IP. The integration then keeps trying the stale ``host`` stored in
the config entry, the device goes unavailable, and the user has to reconfigure
it by hand.

Tuya devices do not all announce themselves unprompted -- in particular
protocol 3.4/3.5 devices stay silent until they receive a discovery request
broadcast to UDP port 7000, at which point they reply with their id (``gwId``),
current IP and ``productKey``. ``tinytuya``'s scanner sends exactly that request,
so ``tinytuya.find_device``/``tinytuya.deviceScan`` locate devices regardless of
how their IP changed. This is the same mechanism the config flow already uses via
``scan_for_device`` and the one ``localtuya`` uses to find devices in seconds.

This module runs two active-scan tasks:

- a fast sweep (every ``SWEEP_INTERVAL``) that relocates *unreachable* configured
  devices: it looks up the current IP by device id and updates the config entry's
  host in place. The existing update-listener reload then reconnects the device on
  the new IP -- no manual reconfiguration, no cloud round-trip, history preserved.
  Reachable devices are never scanned, so there is no traffic while healthy.
- a slower full scan (every ``SCAN_INTERVAL``) that, from a single
  ``deviceScan``: (a) warns, once per device per HA start, when a *configured*
  device reports a ``productKey`` its config file does not list under
  ``products`` (so the config can be improved); and (b) raises an
  ``integration_discovery`` flow for each *unconfigured* device found, so it
  surfaces in Home Assistant for one-click setup (with the built-in ignore).

References:
- tinytuya scanner discovery request (port 7000 for v3.5 devices):
  https://github.com/jasonacox/tinytuya/blob/master/tinytuya/scanner.py
- the integration's own config-flow scan: config_flow.scan_for_device
"""

import asyncio
import io
import logging
from contextlib import redirect_stdout
from datetime import timedelta
from ipaddress import ip_address
from time import monotonic

import tinytuya
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from tinytuya import scanner

from .const import (
    API_PROTOCOL_VERSIONS,
    CLOUD_ACCOUNT_TYPE,
    CONF_DEVICE_CID,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    DATA_CLOUD_IMPORTING,
    DATA_DISCOVERY,
    DOMAIN,
)
from .helpers.config import get_device_id
from .helpers.device_config import get_config

_LOGGER = logging.getLogger(__name__)
_SCAN_LOCK = asyncio.Lock()

# How often to look for unreachable devices. Reachable devices are skipped, so
# a healthy system generates no scan traffic; an unreachable device is normally
# relocated on the first sweep after it drops.
SWEEP_INTERVAL = timedelta(seconds=60)

# How often to run the full network scan (product-id check + new-device
# discovery). Infrequent, since neither action is time critical.
SCAN_INTERVAL = timedelta(minutes=10)
UNCHANGED_SWEEP_BACKOFF = timedelta(minutes=10)


def _find_device(device_id):
    """Locate a device by id on the LAN (blocking; run in executor).

    Sends the Tuya discovery request and returns the scanner result dict
    (``{'ip': ..., 'id': ..., 'product_id': ..., ...}``), or a blank result on
    any socket error.
    """
    try:
        return tinytuya.find_device(dev_id=device_id)
    except OSError:
        return {"ip": None}


def _scan_all():
    """Scan the LAN for all Tuya devices (blocking; run in executor).

    Returns tinytuya's dict keyed by IP, each value carrying ``gwId``,
    ``productKey`` and ``version``; an empty dict on any socket error.
    """
    try:
        return tinytuya.deviceScan(verbose=False, poll=False)
    except OSError:
        return {}


def _force_scan(networks, devices):
    """Probe known private subnets using cloud device IDs and local keys."""
    known = [
        {
            "id": device["id"],
            "key": device[CONF_LOCAL_KEY],
            "name": device.get("name", ""),
        }
        for device in devices
        if device.get("id") and device.get(CONF_LOCAL_KEY)
    ]
    if not known or not networks:
        return {}
    # TinyTuya prints low-level probe responses even with verbose disabled.
    # Keep those encrypted payloads out of Home Assistant logs.
    with redirect_stdout(io.StringIO()):
        return scanner.devices(
            verbose=False,
            color=False,
            poll=False,
            forcescan=networks,
            discover=False,
            byID=False,
            assume_yes=True,
            tuyadevices=known,
        )


async def async_scan_devices(hass: HomeAssistant):
    """Run one LAN scan without competing for TinyTuya's UDP sockets."""
    async with _SCAN_LOCK:
        scan = hass.async_add_executor_job(_scan_all)
        try:
            return await asyncio.shield(scan)
        except asyncio.CancelledError:
            await scan
            raise


async def async_find_device(hass: HomeAssistant, device_id):
    """Locate one device without competing for TinyTuya's UDP sockets."""
    async with _SCAN_LOCK:
        scan = hass.async_add_executor_job(_find_device, device_id)
        try:
            return await asyncio.shield(scan)
        except asyncio.CancelledError:
            await scan
            raise


async def async_force_scan_devices(hass: HomeAssistant, devices, networks):
    """Force-scan known LAN ranges without competing for TinyTuya sockets."""
    async with _SCAN_LOCK:
        scan = hass.async_add_executor_job(_force_scan, networks, devices)
        try:
            return await asyncio.shield(scan)
        except asyncio.CancelledError:
            await scan
            raise


def _set_protocol(device, version) -> None:
    """Configure a TinyTuya device, including device22 pseudo versions."""
    device.disabledetect = True
    if version == 3.22:
        version = 3.3
        device.disabledetect = False
    elif version == 3.42:
        version = 3.4
        device.disabledetect = False
    elif version == 3.52:
        version = 3.5
        device.disabledetect = False
    device.set_version(version)


def _validate_candidate(config, candidate_ip) -> bool:
    """Verify a discovered address by completing an authenticated local request."""
    try:
        address = ip_address(candidate_ip)
    except ValueError:
        return False
    if not address.is_private or address.is_loopback or address.is_unspecified:
        return False

    device_id = config.get(CONF_DEVICE_ID)
    local_key = config.get(CONF_LOCAL_KEY)
    configured = config.get(CONF_PROTOCOL_VERSION, "auto")
    if not device_id or not local_key:
        return False

    protocols = API_PROTOCOL_VERSIONS if configured == "auto" else [configured]
    protocols = [protocol for protocol in protocols if protocol != 3.1]
    for protocol in protocols:
        parent = tinytuya.Device(device_id, candidate_ip, local_key)
        target = parent
        child_id = config.get(CONF_DEVICE_CID)
        if child_id:
            target = tinytuya.Device(child_id, cid=child_id, parent=parent)
        target.set_socketRetryLimit(1)
        if target.parent:
            target.parent.set_socketRetryLimit(1)
        try:
            _set_protocol(target, protocol)
            if target.parent:
                _set_protocol(target.parent, protocol)
            result = target.status()
            if isinstance(result, dict) and (
                "Error" not in result
                and "Err" not in result
                and isinstance(result.get("dps"), dict)
            ):
                return True
        except Exception as exc:  # network/protocol errors reject candidate
            _LOGGER.debug(
                "Candidate %s failed validation with protocol %s: %s",
                candidate_ip,
                protocol,
                exc,
            )
        finally:
            target.set_socketPersistent(False)
            if target.parent:
                target.parent.set_socketPersistent(False)
    return False


class TuyaLANRediscovery:
    """Active LAN discovery for Tuya devices."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._unsub_sweep = None
        self._unsub_scan = None
        self._scanning = False
        self._stopped = False
        self._unchanged_until = {}
        # device ids already warned about an unmatched product id this run.
        self._warned_products = set()
        # gwIds an integration_discovery flow has already been raised for.
        self._discovered = set()

    @callback
    def async_start(self) -> None:
        """Begin periodic discovery tasks."""
        self._stopped = False
        if self._unsub_sweep is None:
            self._unsub_sweep = async_track_time_interval(
                self._hass, self._async_sweep, SWEEP_INTERVAL
            )
        if self._unsub_scan is None:
            self._unsub_scan = async_track_time_interval(
                self._hass, self._async_discovery_scan, SCAN_INTERVAL
            )

    @callback
    def async_stop(self, event=None) -> None:
        """Stop periodic discovery tasks."""
        self._stopped = True
        for attr in ("_unsub_sweep", "_unsub_scan"):
            unsub = getattr(self, attr)
            if unsub is not None:
                unsub()
                setattr(self, attr, None)

    def _unreachable_gateways(self):
        """Yield one entry group per gateway when no sibling returns state."""
        domain_data = self._hass.data.get(DOMAIN, {})
        grouped = {}
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            device_id = entry.data.get(CONF_DEVICE_ID)
            if not device_id:
                continue
            grouped.setdefault(device_id, []).append(entry)

        now = monotonic()
        for device_id, entries in grouped.items():
            if self._unchanged_until.get(device_id, 0) > now:
                continue
            devices = []
            for entry in entries:
                bucket = domain_data.get(get_device_id(entry.data))
                if bucket and (device := bucket.get("device")) is not None:
                    devices.append(device)
            if any(device.has_returned_state for device in devices):
                continue
            yield entries, device_id

    async def _async_sweep(self, now=None) -> None:
        """Scan for any unreachable devices and update changed hosts."""
        if self._scanning:
            return
        targets = list(self._unreachable_gateways())
        if not targets:
            return

        self._scanning = True
        try:
            for entries, device_id in targets:
                found = await async_find_device(self._hass, device_id)
                if self._stopped:
                    return
                ip = found.get("ip") if found else None
                if not ip:
                    continue
                effective_hosts = {
                    {**entry.data, **entry.options}.get(CONF_HOST) for entry in entries
                }
                if effective_hosts == {ip}:
                    self._unchanged_until[device_id] = (
                        monotonic() + UNCHANGED_SWEEP_BACKOFF.total_seconds()
                    )
                    continue
                validation_entry = next(
                    (entry for entry in entries if not entry.data.get(CONF_DEVICE_CID)),
                    entries[0],
                )
                config = {**validation_entry.data, **validation_entry.options}
                valid = await self._hass.async_add_executor_job(
                    _validate_candidate, config, ip
                )
                if self._stopped:
                    return
                if not valid:
                    _LOGGER.warning(
                        "%s: ignoring unverified LAN address %s discovered for device %s",
                        validation_entry.title,
                        ip,
                        device_id,
                    )
                    continue
                # WARNING, not INFO: an IP change is a notable operational event
                # the user may want to see, and config entries commonly run at
                # log level WARNING (which would suppress INFO).
                self._update_cached_gateway(device_id, ip)
                # Write the new host wherever it currently takes effect: always
                # to data, and also to options when options carries the host
                # (the options flow stores it there, overriding data), so the
                # merged config actually changes and the entry reloads.
                for entry in entries:
                    current = {**entry.data, **entry.options}.get(CONF_HOST)
                    if current == ip:
                        continue
                    _LOGGER.warning(
                        "%s: LAN IP changed to %s (was %s); updating configuration",
                        entry.title,
                        ip,
                        current,
                    )
                    new_options = entry.options
                    if CONF_HOST in entry.options:
                        new_options = {**entry.options, CONF_HOST: ip}
                    self._hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, CONF_HOST: ip},
                        options=new_options,
                    )
        finally:
            self._scanning = False

    def _update_cached_gateway(self, device_id, ip) -> None:
        """Point a shared TinyTuya parent at its verified new address."""
        bucket = self._hass.data.get(DOMAIN, {}).get(device_id)
        if not bucket or not (api := bucket.get("tuyadevice")):
            return
        parent = api.parent or api
        parent.set_socketPersistent(False)
        parent.address = ip

    async def _async_discovery_scan(self, now=None) -> None:
        """Full LAN scan: product-id check for known devices, discover new ones."""
        if self._scanning:
            return
        self._scanning = True
        try:
            found = await async_scan_devices(self._hass)
            if self._stopped:
                return
            if not found:
                return

            by_gwid = {}
            for info in found.values():
                gwid = info.get("gwId")
                if gwid:
                    by_gwid[gwid] = info

            configured = {}
            for entry in self._hass.config_entries.async_entries(DOMAIN):
                device_id = entry.data.get(CONF_DEVICE_ID)
                if device_id:
                    configured.setdefault(device_id, []).append(entry)

            for gwid, info in by_gwid.items():
                entries = configured.get(gwid)
                if entries is not None:
                    # The Wi-Fi product ID belongs to the gateway, not a child.
                    direct_entry = next(
                        (
                            entry
                            for entry in entries
                            if not entry.data.get(CONF_DEVICE_CID)
                        ),
                        None,
                    )
                    if direct_entry:
                        await self._check_product(direct_entry, info.get("productKey"))
                else:
                    self._discover_new(gwid, info)
        finally:
            self._scanning = False

    async def _check_product(self, entry, product_id) -> None:
        """Warn once per run when a configured device's product id is unlisted."""
        device_id = entry.data.get(CONF_DEVICE_ID)
        config_type = entry.data.get(CONF_TYPE)
        if not product_id or not config_type or device_id in self._warned_products:
            return
        config = await self._hass.async_add_executor_job(get_config, config_type)
        if config is None or config.matches_product(product_id):
            return
        # WARNING so it is visible under HA's default log level; once per device
        # per run to avoid noise.
        self._warned_products.add(device_id)
        _LOGGER.warning(
            "%s: device product id %s is not listed in its config (%s); "
            "if your device is an exact match for the config please report it so support can be improved",
            entry.title,
            product_id,
            config_type,
        )

    @callback
    def _discover_new(self, gwid, info) -> None:
        """Raise an integration_discovery flow for a not-yet-configured device."""
        domain_data = self._hass.data.get(DOMAIN, {})
        cloud_sync_enabled = any(
            entry.data.get(CONF_TYPE) == CLOUD_ACCOUNT_TYPE
            for entry in self._hass.config_entries.async_entries(DOMAIN)
        )
        if domain_data.get(DATA_CLOUD_IMPORTING) or cloud_sync_enabled:
            return
        if gwid in self._discovered:
            return
        self._discovered.add(gwid)
        self._hass.async_create_task(
            self._hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data={
                    CONF_DEVICE_ID: gwid,
                    CONF_HOST: info.get("ip"),
                    "product_id": info.get("productKey"),
                    "version": info.get("version"),
                },
            )
        )


async def async_start_discovery(hass: HomeAssistant) -> None:
    """Start the shared LAN discovery service if not already running."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_DISCOVERY) is not None:
        return

    rediscovery = TuyaLANRediscovery(hass)
    domain_data[DATA_DISCOVERY] = rediscovery
    rediscovery.async_start()


@callback
def async_stop_discovery(hass: HomeAssistant) -> None:
    """Stop the shared LAN discovery service if running."""
    domain_data = hass.data.get(DOMAIN, {})
    rediscovery = domain_data.pop(DATA_DISCOVERY, None)
    if rediscovery is not None:
        rediscovery.async_stop()
