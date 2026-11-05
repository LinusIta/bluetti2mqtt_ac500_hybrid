"""BLE SOC FC06 ACK is not hardware confirmation; only subsequent TCP state is."""
import ast
import asyncio
import logging
import unittest
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "rootfs" / "app" / "bridge.py"
TREE = ast.parse(SOURCE.read_text())


class ReadableRegisters:
    def __init__(self, address, count):
        self.address = address
    def parse_response(self, value):
        return value


class WriteableRegister:
    def __init__(self, address, value):
        self.address, self.value = address, value
    def __bytes__(self):
        return bytes((6, self.address >> 8, self.address & 255,
                      self.value >> 8, self.value & 255))


class FakeDeviceReader:
    def __init__(self, *args, **kwargs):
        self.value = 100
        self.writes = []
        self.reads = 0
    async def _async_send_command(self, register):
        if isinstance(register, WriteableRegister):
            self.writes.append(register.value)
            return bytes(register)
        self.reads += 1
        return self.value.to_bytes(2, "big")


class FakeCommands:
    def __init__(self):
        self.confirmed = []
    def mark_verified(self, addr, value, stamp):
        self.confirmed.append((addr, value, stamp))


def load_class():
    cls = next(n for n in TREE.body if isinstance(n, ast.ClassDef)
               and n.name == "OneWriteReader")
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    ns = {"DeviceReader": FakeDeviceReader, "WriteableRegister": WriteableRegister,
          "ReadableRegisters": ReadableRegisters, "LOG": logging.getLogger("soc-test")}
    exec(compile(module, str(SOURCE), "exec"), ns)
    return ns["OneWriteReader"]


def load_reconciler():
    cls = next(n for n in TREE.body if isinstance(n, ast.ClassDef)
               and n.name == "Bridge")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == "reconcile_soc_tcp")
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    ns = {"LOG": logging.getLogger("soc-test")}
    exec(compile(module, str(SOURCE), "exec"), ns)
    return ns["reconcile_soc_tcp"]


class TestSocFastAck(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_enabled_skips_extra_ble_read(self):
        reader = load_class()(address=3015, new_value=20, tcp_readback=True)
        await reader._async_send_command(ReadableRegisters(3015, 1))
        self.assertEqual(reader.reads, 1)
        self.assertEqual(reader.writes, [20])
        self.assertTrue(reader.response_ok)
        self.assertIsNone(reader.verified_value)

    async def test_tcp_disabled_retains_ble_verification(self):
        reader = load_class()(address=3015, new_value=20, tcp_readback=False)
        await reader._async_send_command(ReadableRegisters(3015, 1))
        self.assertEqual(reader.reads, 2)
        self.assertEqual(reader.writes, [20])

    async def test_same_value_needs_no_write_and_is_hardware_verified(self):
        reader = load_class()(address=3016, new_value=100, tcp_readback=True)
        await reader._async_send_command(ReadableRegisters(3016, 1))
        self.assertEqual(reader.writes, [])
        self.assertEqual(reader.verified_value, 100)


class TestSocReconcile(unittest.TestCase):
    def setUp(self):
        self.bridge = type("BridgeStub", (), {})()
        self.bridge.commands = FakeCommands()
        self.bridge.soc_tcp_pending = {3015: (20, 100.0)}
        self.reconcile = load_reconciler()

    def test_unchanged_sample_cannot_confirm(self):
        self.reconcile(self.bridge, {"battery_range_start": 100}, 101.0)
        self.assertEqual(self.bridge.commands.confirmed, [])
        self.assertEqual(self.bridge.soc_tcp_pending[3015][0], 20)

    def test_pre_ack_sample_cannot_confirm(self):
        self.reconcile(self.bridge, {"battery_range_start": 20}, 99.0)
        self.assertEqual(self.bridge.commands.confirmed, [])

    def test_post_ack_sample_confirms_and_clears(self):
        self.reconcile(self.bridge, {"battery_range_start": 20}, 103.0)
        self.assertEqual(self.bridge.commands.confirmed, [(3015, 20, 103.0)])
        self.assertEqual(self.bridge.soc_tcp_pending, {})

    def test_timeout_does_not_claim_confirmation(self):
        self.reconcile(self.bridge, {"battery_range_start": 100}, 121.0)
        self.assertEqual(self.bridge.commands.confirmed, [])
        self.assertEqual(self.bridge.soc_tcp_pending, {})

    def test_newer_target_replaces_old_without_false_confirmation(self):
        self.bridge.soc_tcp_pending[3015] = (100, 105.0)
        self.reconcile(self.bridge, {"battery_range_start": 20}, 106.0)
        self.assertEqual(self.bridge.commands.confirmed, [])
        self.reconcile(self.bridge, {"battery_range_start": 100}, 108.0)
        self.assertEqual(self.bridge.commands.confirmed, [(3015, 100, 108.0)])


if __name__ == "__main__":
    unittest.main()
