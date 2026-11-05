"""Fake-server tests for Hybrid's AC500 read-only Modbus TCP driver."""
import asyncio
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "rootfs", "app")))
from hybrid_modbus import AC500ModbusReader


MAP = {
    50002: [120],
    50004: [112],
    50214: [4998, 150],
    50217: [123],
    50269: [45, 102],
    50273: [67],
    51002: [526, 20, 32],
    57001: [1],
    57005: [0],
    57016: [20, 100],
}


class TestAC500Modbus(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        async def handle(inp, out):
            try:
                while True:
                    packet = await inp.readexactly(12)
                    tid, protocol, length, unit, fc, reg, count = struct.unpack(">HHHBBHH", packet)
                    self.requests.append((reg, count, fc, unit))
                    self.assertEqual((protocol, length, unit, fc), (0, 6, 1, 3))
                    vals = MAP.get(reg)
                    if vals is None or len(vals) != count:
                        pdu = bytes([0x83, 0x02])
                    else:
                        raw = struct.pack(">" + "H" * len(vals), *vals)
                        pdu = bytes([3, len(raw)]) + raw
                    out.write(struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu)
                    await out.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                out.close()
                await out.wait_closed()
        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.host, self.port = self.server.sockets[0].getsockname()[:2]
        self.client = AC500ModbusReader(self.host, self.port)

    async def asyncTearDown(self):
        await self.client.close()
        self.server.close()
        await self.server.wait_closed()

    async def test_read_fields(self):
        result = await self.client.read_fields()
        self.assertEqual(result["total_battery_percent"], 32)
        self.assertEqual(result["total_battery_voltage"], 52.6)
        self.assertEqual(result["total_battery_current"], 2.0)
        self.assertEqual(result["ac_input_frequency"], 49.98)
        self.assertEqual(result["ac_input_power"], 150)
        self.assertEqual(result["dc_input_power1"], 45)
        self.assertEqual(result["dc_input_voltage1"], 10.2)
        self.assertEqual(result["dc_input_power2"], 67)
        self.assertEqual(result["dc_input_power"], 112)
        self.assertEqual(result["ac_output_power"], 123)
        self.assertEqual(result["battery_range_start"], 20)
        self.assertEqual(result["battery_range_end"], 100)
        self.assertEqual(result["ac_output_on"], "ON")
        self.assertEqual(result["dc_output_on"], "OFF")
        self.assertEqual(len(self.requests), 10)

    async def test_exception_is_not_a_valid_measurement(self):
        with self.assertRaisesRegex(ValueError, "exception code 2"):
            await self.client.holding(50123, 1)

    async def test_reuse_connection(self):
        first = await self.client.read_fields()
        second = await self.client.read_fields()
        self.assertEqual(first, second)
        self.assertEqual(len(self.requests), 20)


if __name__ == "__main__":
    unittest.main()
