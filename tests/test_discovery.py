"""Tests for the active Tuya LAN rediscovery sweeper."""

import logging
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_HOST
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_local import async_setup, discovery
from custom_components.tuya_local.const import (
    CLOUD_ACCOUNT_TYPE,
    CONF_DEVICE_CID,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    DATA_CLOUD_IMPORTING,
    DATA_DISCOVERY,
    DOMAIN,
)
from custom_components.tuya_local.discovery import (
    TuyaLANRediscovery,
    async_start_discovery,
    async_stop_discovery,
)

TESTKEY = ")<jO<@)'P1|kR$Kd"
DEVID = "bf1234567890abcdef"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _make_entry(hass, host="192.168.1.10", options=None, cid=None, title="thermostat"):
    data = {
        CONF_DEVICE_ID: DEVID,
        CONF_HOST: host,
        CONF_LOCAL_KEY: TESTKEY,
        CONF_POLL_ONLY: False,
        CONF_PROTOCOL_VERSION: "auto",
        CONF_TYPE: "polytherm_polyalpha_thermostat",
    }
    if cid:
        data[CONF_DEVICE_CID] = cid
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=13,
        minor_version=20,
        title=title,
        data=data,
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


def _set_device(hass, returned_state, device_id=DEVID):
    """Register a fake device object in hass.data under the device id."""
    device = type("Dev", (), {"has_returned_state": returned_state})()
    hass.data.setdefault(DOMAIN, {})[device_id] = {"device": device}
    return device


@pytest.mark.asyncio
async def test_sweep_updates_unreachable_changed_host(hass, caplog, mocker):
    """An unreachable device gets relocated, its host updated, and it's logged at WARNING."""
    entry = _make_entry(hass, host="192.168.1.10")
    _set_device(hass, returned_state=False)
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": "192.168.1.55", "id": DEVID},
    )
    mocker.patch(
        "custom_components.tuya_local.discovery._validate_candidate",
        return_value=True,
    )

    with caplog.at_level(
        logging.WARNING, logger="custom_components.tuya_local.discovery"
    ):
        await TuyaLANRediscovery(hass)._async_sweep()
        await hass.async_block_till_done()

    assert entry.data[CONF_HOST] == "192.168.1.55"
    # The IP change must be visible even when the entry runs at WARNING.
    assert "192.168.1.55" in caplog.text
    assert "192.168.1.10" in caplog.text


@pytest.mark.asyncio
async def test_sweep_skips_reachable_device(hass, mocker):
    """A device that is returning state is never scanned."""
    entry = _make_entry(hass, host="192.168.1.10")
    _set_device(hass, returned_state=True)
    find = mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": "192.168.1.55"},
    )

    await TuyaLANRediscovery(hass)._async_sweep()
    await hass.async_block_till_done()

    find.assert_not_called()
    assert entry.data[CONF_HOST] == "192.168.1.10"


@pytest.mark.asyncio
async def test_sweep_no_change_when_ip_same(hass, mocker):
    """If the scan returns the current IP, no entry update happens."""
    entry = _make_entry(hass, host="192.168.1.10")
    _set_device(hass, returned_state=False)
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": "192.168.1.10"},
    )
    update = mocker.spy(hass.config_entries, "async_update_entry")

    await TuyaLANRediscovery(hass)._async_sweep()
    await hass.async_block_till_done()

    update.assert_not_called()
    assert entry.data[CONF_HOST] == "192.168.1.10"


@pytest.mark.asyncio
async def test_sweep_handles_not_found(hass, mocker):
    """A scan that finds nothing must not raise or change anything."""
    entry = _make_entry(hass, host="192.168.1.10")
    _set_device(hass, returned_state=False)
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": None},
    )
    update = mocker.spy(hass.config_entries, "async_update_entry")

    await TuyaLANRediscovery(hass)._async_sweep()
    await hass.async_block_till_done()

    update.assert_not_called()
    assert entry.data[CONF_HOST] == "192.168.1.10"


@pytest.mark.asyncio
async def test_sweep_updates_host_stored_in_options(hass, mocker):
    """When the effective host lives in options, the update targets options."""
    entry = _make_entry(hass, host="10.0.0.1", options={CONF_HOST: "192.168.1.103"})
    _set_device(hass, returned_state=False)
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": "192.168.1.55"},
    )
    mocker.patch(
        "custom_components.tuya_local.discovery._validate_candidate",
        return_value=True,
    )

    await TuyaLANRediscovery(hass)._async_sweep()
    await hass.async_block_till_done()

    assert entry.options[CONF_HOST] == "192.168.1.55"
    assert entry.data[CONF_HOST] == "192.168.1.55"


@pytest.mark.asyncio
async def test_sweep_scans_when_no_device_object(hass, mocker):
    """An entry with no device object yet (failed setup) is still scanned."""
    entry = _make_entry(hass, host="192.168.1.10")
    hass.data.setdefault(DOMAIN, {})  # no device bucket registered
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": "192.168.1.77"},
    )
    mocker.patch(
        "custom_components.tuya_local.discovery._validate_candidate",
        return_value=True,
    )

    await TuyaLANRediscovery(hass)._async_sweep()
    await hass.async_block_till_done()

    assert entry.data[CONF_HOST] == "192.168.1.77"


@pytest.mark.asyncio
async def test_sweep_updates_gateway_siblings_once(hass, mocker):
    """Child entries sharing a gateway relocate together after one LAN lookup."""
    first = _make_entry(hass, cid="child-1", title="child one")
    second = _make_entry(hass, cid="child-2", title="child two")
    _set_device(hass, returned_state=False, device_id=f"{DEVID}/child-1")
    _set_device(hass, returned_state=False, device_id=f"{DEVID}/child-2")
    parent = mocker.MagicMock()
    parent.parent = None
    hass.data[DOMAIN][DEVID] = {"tuyadevice": parent}
    find = mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": "192.168.1.55", "id": DEVID},
    )
    mocker.patch(
        "custom_components.tuya_local.discovery._validate_candidate",
        return_value=True,
    )

    await TuyaLANRediscovery(hass)._async_sweep()
    await hass.async_block_till_done()

    find.assert_called_once()
    assert first.data[CONF_HOST] == "192.168.1.55"
    assert second.data[CONF_HOST] == "192.168.1.55"
    assert parent.address == "192.168.1.55"
    parent.set_socketPersistent.assert_called_once_with(False)


@pytest.mark.asyncio
async def test_sweep_skips_gateway_when_sibling_is_healthy(hass, mocker):
    """One healthy child proves the shared gateway did not move."""
    _make_entry(hass, cid="child-1", title="child one")
    _make_entry(hass, cid="child-2", title="child two")
    _set_device(hass, returned_state=False, device_id=f"{DEVID}/child-1")
    _set_device(hass, returned_state=True, device_id=f"{DEVID}/child-2")
    find = mocker.patch("custom_components.tuya_local.discovery._find_device")

    await TuyaLANRediscovery(hass)._async_sweep()

    find.assert_not_called()


@pytest.mark.asyncio
async def test_stopped_sweep_does_not_update_entry(hass, mocker):
    """A scan completing after shutdown cannot mutate configuration."""
    entry = _make_entry(hass)
    _set_device(hass, returned_state=False)
    rediscovery = TuyaLANRediscovery(hass)

    async def stop_during_scan(*args):
        rediscovery.async_stop()
        return {"ip": "192.168.1.55", "id": DEVID}

    mocker.patch(
        "custom_components.tuya_local.discovery.async_find_device",
        side_effect=stop_during_scan,
    )

    await rediscovery._async_sweep()

    assert entry.data[CONF_HOST] == "192.168.1.10"


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_cancels(hass, mocker):
    """async_start_discovery schedules the sweep + scan intervals; stop cancels both."""
    unsub_sweep = mocker.MagicMock()
    unsub_scan = mocker.MagicMock()
    track = mocker.patch(
        "custom_components.tuya_local.discovery.async_track_time_interval",
        side_effect=[unsub_sweep, unsub_scan],
    )

    await async_start_discovery(hass)
    rediscovery = hass.data[DOMAIN][DATA_DISCOVERY]
    assert isinstance(rediscovery, TuyaLANRediscovery)
    assert track.call_count == 2

    # Second call must not schedule more intervals (singleton).
    await async_start_discovery(hass)
    assert track.call_count == 2

    async_stop_discovery(hass)
    unsub_sweep.assert_called_once()
    unsub_scan.assert_called_once()
    assert DATA_DISCOVERY not in hass.data[DOMAIN]


def _fake_config(matches):
    """Minimal stand-in for a device config with a matches_product() method."""
    return type("Cfg", (), {"matches_product": lambda self, pid: matches})()


def _scan_result(gwid=DEVID, product="keyabc123", ip="192.168.1.10"):
    """A tinytuya.deviceScan-style result: keyed by IP, carrying gwId/productKey."""
    info = {"gwId": gwid, "ip": ip, "version": "3.5"}
    if product is not None:
        info["productKey"] = product
    return {ip: info}


def _patch_flow_init(hass, mocker):
    """Patch the config-entries flow init with an awaitable mock."""
    return mocker.patch.object(
        hass.config_entries.flow, "async_init", new_callable=AsyncMock
    )


@pytest.mark.asyncio
async def test_product_scan_warns_once_on_unmatched_product(hass, caplog, mocker):
    """An unmatched product id is logged at WARNING, once per device per run."""
    _make_entry(hass, host="192.168.1.10")
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(),
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.get_config",
        return_value=_fake_config(False),
    )
    _patch_flow_init(hass, mocker)
    disc = TuyaLANRediscovery(hass)

    with caplog.at_level(
        logging.WARNING, logger="custom_components.tuya_local.discovery"
    ):
        await disc._async_discovery_scan()
        await hass.async_block_till_done()
        assert caplog.text.count("keyabc123") == 1
        # A second scan must not warn again for the same device.
        caplog.clear()
        await disc._async_discovery_scan()
        await hass.async_block_till_done()
        assert "keyabc123" not in caplog.text


@pytest.mark.asyncio
async def test_product_scan_silent_when_product_matches(hass, caplog, mocker):
    """No warning when the product id is listed in the config."""
    _make_entry(hass, host="192.168.1.10")
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(),
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.get_config",
        return_value=_fake_config(True),
    )
    _patch_flow_init(hass, mocker)
    with caplog.at_level(
        logging.WARNING, logger="custom_components.tuya_local.discovery"
    ):
        await TuyaLANRediscovery(hass)._async_discovery_scan()
        await hass.async_block_till_done()
    assert "is not listed" not in caplog.text


@pytest.mark.asyncio
async def test_product_scan_skips_when_no_product_id(hass, mocker):
    """If the scan reports no product id, the config is not even looked up."""
    _make_entry(hass, host="192.168.1.10")
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(product=None),
    )
    get_config = mocker.patch(
        "custom_components.tuya_local.discovery.get_config",
    )
    _patch_flow_init(hass, mocker)
    await TuyaLANRediscovery(hass)._async_discovery_scan()
    await hass.async_block_till_done()
    get_config.assert_not_called()


@pytest.mark.asyncio
async def test_product_scan_handles_missing_config(hass, caplog, mocker):
    """A missing config file must not warn or raise."""
    _make_entry(hass, host="192.168.1.10")
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(),
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.get_config",
        return_value=None,
    )
    _patch_flow_init(hass, mocker)
    with caplog.at_level(
        logging.WARNING, logger="custom_components.tuya_local.discovery"
    ):
        await TuyaLANRediscovery(hass)._async_discovery_scan()
        await hass.async_block_till_done()
    assert "keyabc123" not in caplog.text


@pytest.mark.asyncio
async def test_discovery_raises_flow_for_unknown_device(hass, mocker):
    """An unconfigured device on the LAN starts an integration_discovery flow."""
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(gwid="bfunknown000000000", ip="192.168.1.99"),
    )
    init = _patch_flow_init(hass, mocker)

    await TuyaLANRediscovery(hass)._async_discovery_scan()
    await hass.async_block_till_done()

    init.assert_awaited_once()
    args, kwargs = init.call_args
    assert args[0] == DOMAIN
    assert kwargs["context"]["source"] == "integration_discovery"
    assert kwargs["data"][CONF_DEVICE_ID] == "bfunknown000000000"
    assert kwargs["data"][CONF_HOST] == "192.168.1.99"


@pytest.mark.asyncio
async def test_discovery_skips_configured_device(hass, mocker):
    """A device already configured is not offered for discovery again."""
    _make_entry(hass, host="192.168.1.10")  # DEVID is configured
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(gwid=DEVID),
    )
    mocker.patch(
        "custom_components.tuya_local.discovery.get_config",
        return_value=_fake_config(True),
    )
    init = _patch_flow_init(hass, mocker)

    await TuyaLANRediscovery(hass)._async_discovery_scan()
    await hass.async_block_till_done()

    init.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_raises_flow_only_once_per_device(hass, mocker):
    """Repeated scans do not spawn duplicate flows for the same new device."""
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(gwid="bfunknown000000000", ip="192.168.1.99"),
    )
    init = _patch_flow_init(hass, mocker)
    disc = TuyaLANRediscovery(hass)

    await disc._async_discovery_scan()
    await hass.async_block_till_done()
    await disc._async_discovery_scan()
    await hass.async_block_till_done()

    assert init.await_count == 1


@pytest.mark.asyncio
async def test_discovery_defers_to_enabled_cloud_sync(hass, mocker):
    """Automatic cloud sync prevents competing interactive discovery flows."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="smartlife_cloud_sync",
        data={CONF_TYPE: CLOUD_ACCOUNT_TYPE},
    ).add_to_hass(hass)
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(gwid="bfunknown000000000", ip="192.168.1.99"),
    )
    init = _patch_flow_init(hass, mocker)

    await TuyaLANRediscovery(hass)._async_discovery_scan()

    init.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_defers_during_initial_bulk_import(hass, mocker):
    """Initial bulk import owns discovery identities until it finishes."""
    hass.data[DOMAIN] = {DATA_CLOUD_IMPORTING: {"bulk-flow"}}
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value=_scan_result(gwid="bfunknown000000000", ip="192.168.1.99"),
    )
    init = _patch_flow_init(hass, mocker)

    await TuyaLANRediscovery(hass)._async_discovery_scan()

    init.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_scan_handles_empty_result(hass, mocker):
    """An empty scan (e.g. socket error) does nothing and does not raise."""
    _make_entry(hass, host="192.168.1.10")
    mocker.patch(
        "custom_components.tuya_local.discovery._scan_all",
        return_value={},
    )
    init = _patch_flow_init(hass, mocker)
    await TuyaLANRediscovery(hass)._async_discovery_scan()
    await hass.async_block_till_done()
    init.assert_not_awaited()


def test_module_exposes_expected_intervals():
    """Guard the cadences against accidental change."""
    assert discovery.SWEEP_INTERVAL.total_seconds() == 60
    assert discovery.SCAN_INTERVAL.total_seconds() == 600


@pytest.mark.asyncio
async def test_sweep_rejects_unverified_candidate(hass, caplog, mocker):
    """A scanner result that does not authenticate must never replace the host."""
    entry = _make_entry(hass, host="192.168.1.10")
    _set_device(hass, returned_state=False)
    mocker.patch(
        "custom_components.tuya_local.discovery._find_device",
        return_value={"ip": "172.18.0.1", "id": DEVID},
    )
    mocker.patch(
        "custom_components.tuya_local.discovery._validate_candidate",
        return_value=False,
    )

    with caplog.at_level(
        logging.WARNING, logger="custom_components.tuya_local.discovery"
    ):
        await TuyaLANRediscovery(hass)._async_sweep()

    assert entry.data[CONF_HOST] == "192.168.1.10"
    assert "ignoring unverified LAN address" in caplog.text


def test_validate_candidate_rejects_public_and_invalid_addresses():
    """Discovery can only relocate devices to valid private LAN addresses."""
    config = {
        CONF_DEVICE_ID: DEVID,
        CONF_LOCAL_KEY: TESTKEY,
        CONF_PROTOCOL_VERSION: 3.3,
    }

    assert not discovery._validate_candidate(config, "not-an-ip")
    assert not discovery._validate_candidate(config, "8.8.8.8")
    assert not discovery._validate_candidate(config, "127.0.0.1")


def test_validate_candidate_requires_authenticated_response(mocker):
    """Only an error-free TinyTuya response verifies a new private address."""
    device_class = mocker.patch(
        "custom_components.tuya_local.discovery.tinytuya.Device"
    )
    device = device_class.return_value
    config = {
        CONF_DEVICE_ID: DEVID,
        CONF_LOCAL_KEY: TESTKEY,
        CONF_PROTOCOL_VERSION: 3.3,
    }
    device.status.return_value = {"dps": {"1": True}}
    assert discovery._validate_candidate(config, "192.168.1.55")

    device.status.return_value = {}
    assert not discovery._validate_candidate(config, "192.168.1.56")

    device.status.return_value = {"Error": "Invalid JSON response", "Err": "900"}
    assert not discovery._validate_candidate(config, "192.168.1.57")

    config[CONF_PROTOCOL_VERSION] = 3.1
    assert not discovery._validate_candidate(config, "192.168.1.58")


def test_validate_candidate_uses_child_id_and_accepts_empty_dps(mocker):
    """Gateway children are authenticated through their CID, even with no DPS."""
    device_class = mocker.patch(
        "custom_components.tuya_local.discovery.tinytuya.Device"
    )
    parent = mocker.MagicMock()
    parent.parent = None
    child = mocker.MagicMock()
    child.parent = parent
    child.status.return_value = {"dps": {}}
    device_class.side_effect = [parent, child]
    config = {
        CONF_DEVICE_ID: DEVID,
        CONF_DEVICE_CID: "child-1",
        CONF_LOCAL_KEY: TESTKEY,
        CONF_PROTOCOL_VERSION: 3.3,
    }

    assert discovery._validate_candidate(config, "192.168.1.55")
    assert device_class.call_args_list == [
        mocker.call(DEVID, "192.168.1.55", TESTKEY),
        mocker.call("child-1", cid="child-1", parent=parent),
    ]


def test_force_scan_uses_keys_without_printing_payloads(mocker, capsys):
    """Force scan passes cloud credentials and suppresses TinyTuya probe output."""
    scan = mocker.patch(
        "custom_components.tuya_local.discovery.scanner.devices",
        side_effect=lambda **kwargs: print("encrypted probe") or {"result": kwargs},
    )

    result = discovery._force_scan(
        ["10.3.30.0/24"],
        [{"id": DEVID, CONF_LOCAL_KEY: TESTKEY, "name": "Test device"}],
    )

    assert capsys.readouterr().out == ""
    assert result["result"]["forcescan"] == ["10.3.30.0/24"]
    assert result["result"]["tuyadevices"] == [
        {"id": DEVID, "key": TESTKEY, "name": "Test device"}
    ]
    scan.assert_called_once()


@pytest.mark.asyncio
async def test_scan_service_runs_in_home_assistant(hass, mocker):
    """The diagnostic service returns the Core-side UDP discovery count."""
    mocker.patch(
        "custom_components.tuya_local.async_load_auth",
        new=AsyncMock(return_value=None),
    )
    scan = mocker.patch(
        "custom_components.tuya_local.async_scan_devices",
        new=AsyncMock(return_value={"10.3.30.10": {}, "10.3.30.11": {}}),
    )
    await async_setup(hass, {})

    response = await hass.services.async_call(
        DOMAIN,
        "scan_devices",
        {},
        blocking=True,
        return_response=True,
    )

    assert response == {"devices": 2}
    scan.assert_awaited_once_with(hass)
