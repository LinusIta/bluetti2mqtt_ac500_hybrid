"""No-BLE regression tests for a bounded AC500 encrypted charge-current session.

Load only the ChargeSessionReader AST to avoid importing Bluetooth/cryptography
into GitHub's lightweight CI runner. Mock the GATT primitive, not the session
logic under test.
"""
import ast
import asyncio
import logging
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "rootfs" / "app"))
from command_mailbox import CommandMailbox


class ReadableRegisters:
    def __init__(self, address, count):
        self.starting_address = address

    def parse_response(self, response):
        return response


class WriteableRegister:
    def __init__(self, address, value):
        self.address = address
        self.value = value

    def __bytes__(self):
        return bytes([0x06, self.address >> 8, self.address & 0xff,
                      self.value >> 8, self.value & 0xff])


class FakeDeviceReader:
    def __init__(self, *args, device=None, **kwargs):
        self.client = type("Client", (), {"is_connected": True})()
        self.value = 5
        self.future_value = None
        self.applies_at = 0.0
        self.writes = []

    async def _async_send_command(self, register):
        await asyncio.sleep(0.002)
        if isinstance(register, WriteableRegister):
            self.writes.append(register.value)
            self.future_value = register.value
            self.applies_at = time.monotonic() + 0.08
            return bytes(register)
        if self.future_value is not None and time.monotonic() >= self.applies_at:
            self.value = self.future_value
            self.future_value = None
        return self.value.to_bytes(2, "big")


class FakeBridge:
    def __init__(self):
        self.commands = CommandMailbox(max_age=45)
        self.last_registers = {}
        self.states = []
        self.stop_event = asyncio.Event()
        self.refresh_requested = asyncio.Event()

    def publish(self, field, value):
        self.states.append((field, value))


def load_reader():
    path = Path(__file__).parents[1] / "rootfs" / "app" / "bridge.py"
    root = ast.parse(path.read_text())
    cls = next(node for node in root.body
               if isinstance(node, ast.ClassDef) and node.name == "ChargeSessionReader")
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    env = {"DeviceReader": FakeDeviceReader, "ReadableRegisters": ReadableRegisters,
           "WriteableRegister": WriteableRegister, "asyncio": asyncio,
           "time": time, "LOG": logging.getLogger("fake-ble")}
    exec(compile(module, str(path), "exec"), env)
    klass = env["ChargeSessionReader"]
    klass.SESSION_SECONDS = 2.05
    klass.VERIFY_DELAY = 0.11
    klass.VERIFY_RETRY = 0.05
    return klass


ChargeSessionReader = load_reader()


class TestChargeSession(unittest.IsolatedAsyncioTestCase):
    async def test_one_write_and_hardware_readback(self):
        bridge = FakeBridge()
        bridge.commands.put(3019, "grid_charge_amps", "4", 4, time.monotonic())
        assert bridge.commands.pop(time.monotonic())[3] == 4
        reader = ChargeSessionReader(bridge=bridge, first_field="grid_charge_amps", first_value=4)
        await reader._async_send_command(ReadableRegisters(3019, 1))
        self.assertEqual(reader.writes, [4])
        self.assertIn(("grid_charge_amps", 4), bridge.states)
        self.assertEqual(reader.verified, 1)
        self.assertEqual(bridge.commands.verified[3019][0], 4)

    async def test_newer_target_reuses_session_without_handshake(self):
        bridge = FakeBridge()
        bridge.commands.put(3019, "grid_charge_amps", "4", 4, time.monotonic())
        bridge.commands.pop(time.monotonic())
        reader = ChargeSessionReader(bridge=bridge, first_field="grid_charge_amps", first_value=4)

        async def change():
            await asyncio.sleep(0.055)
            bridge.commands.put(3019, "grid_charge_amps", "3", 3, time.monotonic())

        await asyncio.gather(reader._async_send_command(ReadableRegisters(3019, 1)), change())
        self.assertEqual(reader.writes, [4, 3])
        self.assertEqual(bridge.commands.verified[3019][0], 3)

    async def test_other_register_interrupts_and_is_not_consumed(self):
        bridge = FakeBridge()
        bridge.commands.put(3019, "grid_charge_amps", "4", 4, time.monotonic())
        bridge.commands.pop(time.monotonic())
        reader = ChargeSessionReader(bridge=bridge, first_field="grid_charge_amps", first_value=4)

        async def other():
            await asyncio.sleep(0.06)
            bridge.commands.put(3015, "battery_range_start", "100", 100, time.monotonic())

        await asyncio.gather(reader._async_send_command(ReadableRegisters(3019, 1)), other())
        self.assertEqual(reader.writes, [4])
        self.assertIn(3015, bridge.commands.pending)

    async def test_identical_inflight_setpoint_does_not_repeat_fc06(self):
        bridge = FakeBridge()
        bridge.commands.put(3019, "grid_charge_amps", "4", 4, time.monotonic())
        bridge.commands.pop(time.monotonic())
        reader = ChargeSessionReader(bridge=bridge, first_field="grid_charge_amps", first_value=4)

        async def duplicate():
            await asyncio.sleep(0.05)
            self.assertEqual(bridge.commands.put(
                3019, "grid_charge_amps", "4", 4, time.monotonic()), "already_active")

        await asyncio.gather(reader._async_send_command(ReadableRegisters(3019, 1)), duplicate())
        self.assertEqual(reader.writes, [4])


if __name__ == "__main__":
    unittest.main()
