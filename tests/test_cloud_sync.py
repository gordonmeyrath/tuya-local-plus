"""Tests for automatic Smart Life cloud synchronization."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_HOST
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_local import cloud_sync
from custom_components.tuya_local.const import (
    CLOUD_ACCOUNT_TYPE,
    CLOUD_INVENTORY_SOURCE,
    CLOUD_PENDING_TYPE,
    CLOUD_SYNC_SOURCE,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _cloud_device(device_id="new-device", **overrides):
    device = {
        "id": device_id,
        "name": "Kitchen plug",
        "product_name": "Plug",
        "product_id": "cloud-product",
        "category": "cz",
        CONF_LOCAL_KEY: "new-local-key",
        "is_hub": False,
        "sub": False,
        "node_id": "",
        "online": True,
        "support_local": True,
    }
    device.update(overrides)
    return device


def _patch_cloud(mocker, devices):
    instance = mocker.MagicMock()
    instance.async_initialize = AsyncMock()
    instance.async_get_devices = AsyncMock(return_value=devices)
    mocker.patch("custom_components.tuya_local.cloud_sync.Cloud", return_value=instance)
    mocker.patch(
        "custom_components.tuya_local.cloud_sync.async_load_auth",
        new=AsyncMock(return_value={"token_info": {"access_token": "token"}}),
    )
    return instance


@pytest.mark.asyncio
async def test_sync_imports_new_direct_device(hass, mocker):
    """A newly paired direct Wi-Fi device is imported without user input."""
    _patch_cloud(mocker, {"new-device": _cloud_device()})
    scan = mocker.patch(
        "custom_components.tuya_local.cloud_sync.async_scan_devices",
        new=AsyncMock(
            return_value={
                "192.168.1.50": {
                    "gwId": "new-device",
                    "ip": "192.168.1.50",
                    "version": "3.4",
                    "productKey": "local-product",
                }
            }
        ),
    )
    flow_init = mocker.patch.object(
        hass.config_entries.flow,
        "async_init",
        new=AsyncMock(return_value={"type": "create_entry"}),
    )

    await cloud_sync.TuyaCloudSync(hass)._async_sync()

    scan.assert_awaited_once()
    flow_init.assert_awaited_once()
    _, kwargs = flow_init.call_args
    assert kwargs["context"]["source"] == CLOUD_SYNC_SOURCE
    assert kwargs["data"][CONF_DEVICE_ID] == "new-device"
    assert kwargs["data"][CONF_HOST] == "192.168.1.50"
    assert kwargs["data"]["product_ids"] == ["cloud-product", "local-product"]
    assert kwargs["data"]["category"] == "cz"


@pytest.mark.asyncio
async def test_sync_force_scans_verified_tuya_subnet(hass, mocker):
    """Cloud sync finds silent devices over TCP on an existing Tuya /24."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="existing-device",
        data={
            CONF_DEVICE_ID: "existing-device",
            CONF_HOST: "10.3.30.10",
            CONF_LOCAL_KEY: "existing-key",
            CONF_PROTOCOL_VERSION: 3.3,
            CONF_POLL_ONLY: False,
            CONF_TYPE: "smartplugv1",
        },
    )
    existing.add_to_hass(hass)
    _patch_cloud(
        mocker,
        {
            "existing-device": _cloud_device(
                "existing-device", local_key="existing-key"
            ),
            "new-device": _cloud_device(),
        },
    )
    mocker.patch(
        "custom_components.tuya_local.cloud_sync.async_scan_devices",
        new=AsyncMock(return_value={}),
    )
    force_scan = mocker.patch(
        "custom_components.tuya_local.cloud_sync.async_force_scan_devices",
        new=AsyncMock(
            return_value={
                "10.3.30.50": {
                    "gwId": "new-device",
                    "ip": "10.3.30.50",
                    "version": "3.4",
                }
            }
        ),
    )
    flow_init = mocker.patch.object(
        hass.config_entries.flow,
        "async_init",
        new=AsyncMock(return_value={"type": "create_entry"}),
    )

    await cloud_sync.TuyaCloudSync(hass)._async_sync()

    assert force_scan.await_args.args[2] == ["10.3.30.0/24"]
    assert force_scan.await_args.args[1][0]["id"] == "new-device"
    assert flow_init.await_args.kwargs["data"][CONF_HOST] == "10.3.30.50"


@pytest.mark.asyncio
async def test_sync_skips_force_scan_when_udp_finds_devices(hass, mocker):
    """TCP probing cannot delay devices already discovered over UDP."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="existing-device",
        data={
            CONF_DEVICE_ID: "existing-device",
            CONF_HOST: "10.3.30.10",
            CONF_LOCAL_KEY: "existing-key",
            CONF_PROTOCOL_VERSION: 3.3,
            CONF_POLL_ONLY: False,
            CONF_TYPE: "smartplugv1",
        },
    )
    existing.add_to_hass(hass)
    _patch_cloud(
        mocker,
        {
            "existing-device": _cloud_device(
                "existing-device", local_key="existing-key"
            ),
            "new-device": _cloud_device(),
            "silent-device": _cloud_device("silent-device"),
        },
    )
    mocker.patch(
        "custom_components.tuya_local.cloud_sync.async_scan_devices",
        new=AsyncMock(
            return_value={
                "10.3.30.50": {
                    "gwId": "new-device",
                    "ip": "10.3.30.50",
                    "version": "3.4",
                }
            }
        ),
    )
    force_scan = mocker.patch(
        "custom_components.tuya_local.cloud_sync.async_force_scan_devices",
        new=AsyncMock(side_effect=ConnectionResetError),
    )
    flow_init = mocker.patch.object(
        hass.config_entries.flow,
        "async_init",
        new=AsyncMock(return_value={"type": "create_entry"}),
    )

    await cloud_sync.TuyaCloudSync(hass)._async_sync()

    force_scan.assert_not_awaited()
    assert flow_init.await_count == 2
    imported = next(
        call
        for call in flow_init.await_args_list
        if call.kwargs["context"]["source"] == CLOUD_SYNC_SOURCE
    )
    inventory = next(
        call
        for call in flow_init.await_args_list
        if call.kwargs["context"]["source"] == CLOUD_INVENTORY_SOURCE
    )
    assert imported.kwargs["data"][CONF_DEVICE_ID] == "new-device"
    assert inventory.kwargs["data"][CONF_DEVICE_ID] == "silent-device"


@pytest.mark.asyncio
async def test_sync_skips_hubs_and_subdevices(hass, mocker):
    """Gateway and child devices become inventory, not direct local imports."""
    _patch_cloud(
        mocker,
        {
            "hub": _cloud_device("hub", is_hub=True),
            "child": _cloud_device("child", sub=True, node_id="node-1"),
        },
    )
    scan = mocker.patch(
        "custom_components.tuya_local.cloud_sync.async_scan_devices",
        new=AsyncMock(),
    )
    flow_init = mocker.patch.object(
        hass.config_entries.flow, "async_init", new=AsyncMock()
    )

    await cloud_sync.TuyaCloudSync(hass)._async_sync()

    scan.assert_not_awaited()
    assert flow_init.await_count == 2
    assert {call.kwargs["context"]["source"] for call in flow_init.await_args_list} == {
        CLOUD_INVENTORY_SOURCE
    }


@pytest.mark.asyncio
async def test_sync_adds_offline_device_to_cloud_inventory(hass, mocker):
    """An offline cloud device is represented before it has a LAN address."""
    _patch_cloud(
        mocker,
        {"offline-device": _cloud_device("offline-device", online=False)},
    )
    mocker.patch(
        "custom_components.tuya_local.cloud_sync.async_scan_devices",
        new=AsyncMock(return_value={}),
    )
    flow_init = mocker.patch.object(
        hass.config_entries.flow,
        "async_init",
        new=AsyncMock(return_value={"type": "create_entry"}),
    )

    await cloud_sync.TuyaCloudSync(hass)._async_sync()

    flow_init.assert_awaited_once()
    data = flow_init.await_args.kwargs["data"]
    assert flow_init.await_args.kwargs["context"]["source"] == CLOUD_INVENTORY_SOURCE
    assert data["cloud_inventory_id"] == "offline-device"
    assert data["pending_reason"] == "offline"
    assert data["cloud_online"] is False


@pytest.mark.asyncio
async def test_sync_skips_existing_entry_without_unique_id(hass, mocker):
    """Legacy or failed entries are matched by device ID, not runtime state."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=None,
        data={
            CONF_DEVICE_ID: "existing-device",
            CONF_HOST: "192.168.1.10",
            CONF_LOCAL_KEY: "same-key",
            CONF_PROTOCOL_VERSION: 3.3,
            CONF_POLL_ONLY: False,
            CONF_TYPE: "smartplugv1",
        },
    )
    entry.add_to_hass(hass)
    _patch_cloud(
        mocker,
        {"existing-device": _cloud_device("existing-device", local_key="same-key")},
    )
    scan = mocker.patch(
        "custom_components.tuya_local.cloud_sync.async_scan_devices",
        new=AsyncMock(),
    )

    await cloud_sync.TuyaCloudSync(hass)._async_sync()

    scan.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_refreshes_changed_local_key(hass, mocker):
    """Re-pairing in Smart Life updates the key without replacing identities."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="existing-device",
        data={
            CONF_DEVICE_ID: "existing-device",
            CONF_HOST: "192.168.1.10",
            CONF_LOCAL_KEY: "old-key",
            CONF_PROTOCOL_VERSION: 3.3,
            CONF_POLL_ONLY: False,
            CONF_TYPE: "smartplugv1",
        },
    )
    entry.add_to_hass(hass)
    _patch_cloud(
        mocker,
        {"existing-device": _cloud_device("existing-device")},
    )

    await cloud_sync.TuyaCloudSync(hass)._async_sync()

    assert entry.data[CONF_LOCAL_KEY] == "new-local-key"
    assert entry.unique_id == "existing-device"


@pytest.mark.asyncio
async def test_sync_waits_for_authentication(hass, mocker):
    """Missing authentication does not perform cloud or LAN operations."""
    mocker.patch(
        "custom_components.tuya_local.cloud_sync.async_load_auth",
        new=AsyncMock(return_value=None),
    )
    cloud = mocker.patch("custom_components.tuya_local.cloud_sync.Cloud")
    scan = mocker.patch(
        "custom_components.tuya_local.cloud_sync.async_scan_devices",
        new=AsyncMock(),
    )

    await cloud_sync.TuyaCloudSync(hass)._async_sync()

    cloud.assert_not_called()
    scan.assert_not_awaited()


def test_cloud_account_is_not_a_physical_device(hass):
    """The synchronization entry is excluded from device duplicate checks."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="smartlife_cloud_sync",
        data={CONF_TYPE: CLOUD_ACCOUNT_TYPE},
    )
    entry.add_to_hass(hass)

    assert cloud_sync._configured_device_ids(hass) == set()


def test_pending_inventory_is_not_locally_configured(hass):
    """Pending cloud inventory remains eligible for later local upgrade."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="pending-device",
        data={
            CONF_TYPE: CLOUD_PENDING_TYPE,
            CONF_DEVICE_ID: "pending-device",
            "cloud_inventory_id": "pending-device",
        },
    )
    entry.add_to_hass(hass)

    assert cloud_sync._configured_device_ids(hass) == set()
    assert cloud_sync._inventory_device_ids(hass) == {"pending-device"}


@pytest.mark.asyncio
async def test_start_cloud_sync_aborts_competing_flows(hass, mocker):
    """Cloud sync releases IDs claimed by stale discovery and import flows."""
    progress = mocker.patch.object(
        hass.config_entries.flow,
        "async_progress_by_handler",
        return_value=[{"flow_id": "discovery-flow"}],
    )
    abort = mocker.patch.object(hass.config_entries.flow, "async_abort")
    start = mocker.patch.object(cloud_sync.TuyaCloudSync, "async_start")

    await cloud_sync.async_start_cloud_sync(hass)

    assert progress.call_count == 2
    assert abort.call_count == 2
    abort.assert_called_with("discovery-flow")
    start.assert_called_once()
