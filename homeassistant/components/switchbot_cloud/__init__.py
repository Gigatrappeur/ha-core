"""SwitchBot via API integration."""

from asyncio import gather
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
from logging import getLogger

from aiohttp import web
from switchbot_api import CannotConnect, Device, InvalidAuth, Remote, SwitchBotAPI

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_API_TOKEN, CONF_WEBHOOK_ID, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, ENTRY_TITLE
from .coordinator import SwitchBotCoordinator

_LOGGER = getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.VACUUM,
]


@dataclass
class SwitchbotDevices:
    """Switchbot devices data."""

    buttons: list[Device] = field(default_factory=list)
    climates: list[Remote] = field(default_factory=list)
    switches: list[Device | Remote] = field(default_factory=list)
    sensors: list[Device] = field(default_factory=list)
    vacuums: list[Device] = field(default_factory=list)
    locks: list[Device] = field(default_factory=list)


@dataclass
class SwitchbotCloudData:
    """Data to use in platforms."""

    api: SwitchBotAPI
    devices: SwitchbotDevices


async def coordinator_for_device(
    hass: HomeAssistant,
    api: SwitchBotAPI,
    device: Device | Remote,
    coordinators_by_id: dict[str, SwitchBotCoordinator],
    update_by_webhook: bool = False,
) -> SwitchBotCoordinator:
    """Instantiate coordinator and adds to list for gathering."""
    coordinator = coordinators_by_id.setdefault(
        device.device_id, SwitchBotCoordinator(hass, api, device, update_by_webhook)
    )

    if coordinator.data is None:
        await coordinator.async_config_entry_first_refresh()

    return coordinator


async def make_switchbot_devices(
    hass: HomeAssistant,
    api: SwitchBotAPI,
    devices: list[Device | Remote],
    coordinators_by_id: dict[str, SwitchBotCoordinator],
) -> SwitchbotDevices:
    """Make SwitchBot devices."""
    devices_data = SwitchbotDevices()
    await gather(
        *[
            make_device_data(hass, api, device, devices_data, coordinators_by_id)
            for device in devices
        ]
    )

    return devices_data


async def make_device_data(
    hass: HomeAssistant,
    api: SwitchBotAPI,
    device: Device | Remote,
    devices_data: SwitchbotDevices,
    coordinators_by_id: dict[str, SwitchBotCoordinator],
) -> None:
    """Make device data."""
    if isinstance(device, Remote) and device.device_type.endswith("Air Conditioner"):
        coordinator = await coordinator_for_device(
            hass, api, device, coordinators_by_id
        )
        devices_data.climates.append((device, coordinator))
    if (
        isinstance(device, Device)
        and (
            device.device_type.startswith("Plug")
            or device.device_type in ["Relay Switch 1PM", "Relay Switch 1"]
        )
    ) or isinstance(device, Remote):
        coordinator = await coordinator_for_device(
            hass, api, device, coordinators_by_id
        )
        devices_data.switches.append((device, coordinator))

    if isinstance(device, Device) and device.device_type in [
        "Meter",
        "MeterPlus",
        "WoIOSensor",
        "Hub 2",
        "MeterPro",
        "MeterPro(CO2)",
        "Relay Switch 1PM",
        "Plug Mini (US)",
        "Plug Mini (JP)",
    ]:
        coordinator = await coordinator_for_device(
            hass, api, device, coordinators_by_id
        )
        devices_data.sensors.append((device, coordinator))

    if isinstance(device, Device) and device.device_type in [
        "K10+",
        "K10+ Pro",
        "Robot Vacuum Cleaner S1",
        "Robot Vacuum Cleaner S1 Plus",
    ]:
        coordinator = await coordinator_for_device(
            hass, api, device, coordinators_by_id, True
        )
        devices_data.vacuums.append((device, coordinator))

    if isinstance(device, Device) and device.device_type.startswith("Smart Lock"):
        coordinator = await coordinator_for_device(
            hass, api, device, coordinators_by_id
        )
        devices_data.locks.append((device, coordinator))

    if isinstance(device, Device) and device.device_type in ["Bot"]:
        coordinator = await coordinator_for_device(
            hass, api, device, coordinators_by_id
        )
        if coordinator.data is not None:
            if coordinator.data.get("deviceMode") == "pressMode":
                devices_data.buttons.append((device, coordinator))
            else:
                devices_data.switches.append((device, coordinator))


async def async_setup_entry(hass: HomeAssistant, config: ConfigEntry) -> bool:
    """Set up SwitchBot via API from a config entry."""
    token = config.data[CONF_API_TOKEN]
    secret = config.data[CONF_API_KEY]

    api = SwitchBotAPI(token=token, secret=secret)
    try:
        devices = await api.list_devices()
    except InvalidAuth as ex:
        _LOGGER.error(
            "Invalid authentication while connecting to SwitchBot API: %s", ex
        )
        return False
    except CannotConnect as ex:
        raise ConfigEntryNotReady from ex
    _LOGGER.debug("Devices: %s", devices)
    coordinators_by_id: dict[str, SwitchBotCoordinator] = {}

    switchbot_devices = await make_switchbot_devices(
        hass, api, devices, coordinators_by_id
    )
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][config.entry_id] = SwitchbotCloudData(
        api=api, devices=switchbot_devices
    )
    await hass.config_entries.async_forward_entry_setups(config, PLATFORMS)

    if any(
        coordinator.update_by_webhook() for coordinator in coordinators_by_id.values()
    ):
        # Need webhook
        if CONF_WEBHOOK_ID not in config.data or config.unique_id is None:
            new_data = config.data.copy()
            if CONF_WEBHOOK_ID not in new_data:
                new_data[CONF_WEBHOOK_ID] = webhook.async_generate_id()

            hass.config_entries.async_update_entry(
                config,
                data=new_data,
            )

        # register webhook
        webhook_name = ENTRY_TITLE
        if config.title != ENTRY_TITLE:
            webhook_name = f"{ENTRY_TITLE} {config.title}"

        with contextlib.suppress(Exception):
            webhook.async_register(
                hass,
                DOMAIN,
                webhook_name,
                config.data[CONF_WEBHOOK_ID],
                create_handle_webhook(coordinators_by_id),
            )

        webhook_url = webhook.async_generate_url(
            hass,
            config.data[CONF_WEBHOOK_ID],
        )
        # check if webhook is configured
        check_webhook_result = None
        with contextlib.suppress(Exception):
            check_webhook_result = await api.get_webook_configuration()

        actual_webhook_urls = (
            check_webhook_result["urls"]
            if check_webhook_result and "urls" in check_webhook_result
            else []
        )
        need_add_webhook = (
            len(actual_webhook_urls) == 0 or webhook_url not in actual_webhook_urls
        )
        need_clean_previous_webhook = (
            len(actual_webhook_urls) > 0 and webhook_url not in actual_webhook_urls
        )

        if need_clean_previous_webhook:
            # it seems is impossible to register multiple webhook.
            # So, if webhook already exists, we delete it
            await api.delete_webhook(actual_webhook_urls[0])

        if need_add_webhook:
            # call api for register webhookurl
            await api.setup_webhook(webhook_url)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


def create_handle_webhook(
    coordinators_by_id: dict[str, SwitchBotCoordinator],
) -> Callable[[HomeAssistant, str, web.Request], Awaitable[None]]:
    """Create a webhook handler."""

    async def internal_handle_webhook(
        hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> None:
        """Handle webhook callback."""
        data = await request.json()
        _LOGGER.info("Receive data from webhook %s", repr(data))

        if not isinstance(data, dict):
            _LOGGER.error(
                "Received invalid data from switchbot webhook. Data needs to be a dictionary: %s",
                data,
            )
            return

        if "eventType" not in data or data["eventType"] != "changeReport":
            _LOGGER.error(
                'Received invalid data from switchbot webhook. Attribute eventType is missing or not equals to "changeReport": %s',
                data,
            )
            return

        if "eventVersion" not in data or data["eventVersion"] != "1":
            _LOGGER.error(
                'Received invalid data from switchbot webhook. Attribute eventVersion is missing or not equals to "1": %s',
                data,
            )
            return

        if "context" not in data or not isinstance(data["context"], dict):
            _LOGGER.error(
                "Received invalid data from switchbot webhook. Attribute context is missing or not instance of dict: %s",
                data,
            )
            return

        if "deviceType" not in data["context"] or "deviceMac" not in data["context"]:
            _LOGGER.error(
                "Received invalid data from switchbot webhook. Missing deviceType or deviceMac: %s",
                data,
            )
            return

        deviceMac = data["context"]["deviceMac"]

        if deviceMac not in coordinators_by_id:
            _LOGGER.error(
                "Received data for unknown entity from switchbot webhook: %s", data
            )
            return

        coordinators_by_id[deviceMac].async_set_updated_data(data["context"])

    return internal_handle_webhook
