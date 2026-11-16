"""Explicit, one-shot BlueZ adapter reset. Never called automatically."""
import asyncio
import re

from dbus_fast import Message, MessageType, Variant, BusType
from dbus_fast.aio import MessageBus


async def reset_adapter(adapter: str, ble) -> None:
    """Cycle host BlueZ Adapter1 power with the encrypted BLE link closed.

    The add-on must have host_dbus access and sufficient D-Bus permissions.
    This affects every Bluetooth device using the selected adapter.
    """
    if not re.fullmatch(r"hci[0-9]+", adapter):
        raise ValueError("Invalid Bluetooth adapter")

    async with ble.lock:
        await ble.close()
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        path = f"/org/bluez/{adapter}"
        powered_off = False

        async def set_power(enabled: bool) -> None:
            reply = await asyncio.wait_for(bus.call(Message(
                destination="org.bluez",
                path=path,
                interface="org.freedesktop.DBus.Properties",
                member="Set",
                signature="ssv",
                body=["org.bluez.Adapter1", "Powered", Variant("b", enabled)],
            )), timeout=10)
            if reply.message_type == MessageType.ERROR:
                raise RuntimeError(
                    f"BlueZ Adapter1 Powered={enabled} rejected: "
                    f"{reply.error_name} {reply.body}"
                )

        try:
            await set_power(False)
            powered_off = True
            await asyncio.sleep(5)
            await set_power(True)
            powered_off = False
            # Give BlueZ time to rediscover the AC500 after power restoration.
            await asyncio.sleep(3)
        finally:
            if powered_off:
                try:
                    await set_power(True)
                except Exception:
                    # Caller reports the failure; best effort to leave BT on.
                    pass
            bus.disconnect()
