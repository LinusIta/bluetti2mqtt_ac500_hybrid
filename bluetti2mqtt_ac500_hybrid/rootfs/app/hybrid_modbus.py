"""Read-only AC500 Modbus TCP telemetry (unit id 1).

Addresses from bluetti-community/bluetti-modbus AC500 profile. No FC06 or
writes are implemented here; all device commands remain on Secure BLE.
"""
import asyncio
import struct

# BLE state-topic names that Modbus can supply.
MODBUS_FIELDS = frozenset({
    "ac_output_power", "ac_input_power", "dc_input_power",
    "total_battery_percent", "battery_soc", "total_battery_voltage",
    "total_battery_current", "ac_input_frequency", "dc_input_power1",
    "dc_input_voltage1", "dc_input_power2",
    "battery_range_start", "battery_range_end",
    "ac_output_on", "dc_output_on",
})

class AC500ModbusReader:
    def __init__(self, host, port=502):
        self.host = host
        self.port = int(port)
        self.reader = None
        self.writer = None
        self.transaction_id = 0

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (OSError, ConnectionError):
                pass
        self.reader = None
        self.writer = None

    async def _connect(self):
        if self.writer is not None and not self.writer.is_closing():
            return
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=3
        )

    async def holding(self, address, count):
        await self._connect()
        self.transaction_id = (self.transaction_id + 1) & 0xffff
        tx = self.transaction_id
        # MBAP: tx_id, protocol=0, length=6, unit=1; PDU FC03/address/count.
        request = struct.pack(">HHHBBHH", tx, 0, 6, 1, 3, address, count)
        self.writer.write(request)
        await asyncio.wait_for(self.writer.drain(), timeout=3)
        head = await asyncio.wait_for(self.reader.readexactly(7), timeout=3)
        got_tx, proto, length, unit = struct.unpack(">HHHB", head)
        if got_tx != tx or proto != 0 or unit != 1 or length < 3 or length > 253:
            raise ValueError("Invalid Modbus TCP MBAP response")
        pdu = await asyncio.wait_for(self.reader.readexactly(length - 1), timeout=3)
        if pdu[0] == 0x83:
            raise ValueError(f"Modbus exception code {pdu[1]} for register {address}")
        if pdu[0] != 3 or pdu[1] != count * 2 or len(pdu) != 2 + count * 2:
            raise ValueError(f"Invalid Modbus FC03 response for register {address}")
        return struct.unpack(">" + "H" * count, pdu[2:])

    async def read_fields(self):
        # Separate reads avoid undefined intermediate AC500 register holes.
        # Keep the same TCP connection for successive polls (firmware stability).
        ac_total = (await self.holding(50002, 1))[0]
        pv_total = (await self.holding(50004, 1))[0]
        freq, grid_local = await self.holding(50214, 2)
        ac_local = (await self.holding(50217, 1))[0]
        pv1, pv1_voltage = await self.holding(50269, 2)
        pv2 = (await self.holding(50273, 1))[0]
        bat_voltage, bat_current, soc = await self.holding(51002, 3)
        ac_switch = (await self.holding(57001, 1))[0]
        dc_switch = (await self.holding(57005, 1))[0]
        soc_min, soc_max = await self.holding(57016, 2)
        if not (0 <= soc <= 100 and 250 <= bat_voltage <= 700 and (freq == 0 or 4000 <= freq <= 7000)
                and 0 <= soc_min <= 100 and 0 <= soc_max <= 100 and soc_min <= soc_max
                and ac_switch in (0, 1) and dc_switch in (0, 1)):
            raise ValueError("Implausible AC500 Modbus voltage/SOC/frequency")
        values = {
            "ac_output_power": ac_local,
            "ac_input_power": grid_local,
            "dc_input_power": pv_total,
            "total_battery_percent": soc,
            "battery_soc": soc,
            "total_battery_voltage": round(bat_voltage * 0.1, 1),
            "total_battery_current": round(bat_current * 0.1, 1),
            "dc_input_power1": pv1,
            "dc_input_voltage1": round(pv1_voltage * 0.1, 1),
            "dc_input_power2": pv2,
            # Read-only mirrors of BLE 3015/3016. These are live states,
            # not proof of FC06 completion until cross-checked on hardware.
            "battery_range_start": soc_min,
            "battery_range_end": soc_max,
            "ac_output_on": "ON" if ac_switch else "OFF",
            "dc_output_on": "ON" if dc_switch else "OFF",
        }
        # Off-grid devices may report no mains frequency; leave BLE authoritative.
        if freq:
            values["ac_input_frequency"] = round(freq * 0.01, 2)
        return values
