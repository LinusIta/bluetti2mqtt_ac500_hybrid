"""AC500 vendor-encrypted BLE transport, one FC16 per write and FC03 readback."""
import asyncio
import logging
import struct
import time
from bleak import BleakClient

LOG = logging.getLogger("secure_ble")
RX = "0000ff01-0000-1000-8000-00805f9b34fb"
TX = "0000ff02-0000-1000-8000-00805f9b34fb"

def crc16(data):
    crc = 0xffff
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xa001 if crc & 1 else crc >> 1
    return crc

def rtu(fc, address, value):
    payload = struct.pack(">BBHH", 1, fc, address, value)
    return payload + struct.pack("<H", crc16(payload))

def write16(address, value):
    payload = struct.pack(">BBHHBH", 1, 16, address, 1, 2, value)
    return payload + struct.pack("<H", crc16(payload))

class SecureBLE:
    def __init__(self, mac):
        from bluetti_crypt import BluettiCrypt
        self.mac = mac
        # Use a fresh native crypto context for each BLE ECDH session.
        self.crypt_type = BluettiCrypt
        self.crypt = None
        self.client = None
        self.generation = 0
        self.queue = asyncio.Queue()
        self.lock = asyncio.Lock()
        self.authenticated = False
        self.connected_at = 0

    def _notification(self, generation, _sender, data):
        # Ignore late packets from closed connections.
        if generation == self.generation:
            self.queue.put_nowait(bytes(data))

    async def close(self):
        self.generation += 1
        client = self.client
        self.client = None
        self.authenticated = False
        self.connected_at = 0
        self.queue = asyncio.Queue()
        if client:
            try:
                if client.is_connected:
                    await client.stop_notify(RX)
            except Exception:
                LOG.debug("BLE stop_notify error", exc_info=True)
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                LOG.debug("BLE disconnect error", exc_info=True)
        self.crypt = None

    async def _send(self, frame):
        encrypted = self.crypt.encrypt_data(frame)
        if not encrypted:
            raise RuntimeError("Vendor module refused Modbus frame")
        await self.client.write_gatt_char(TX, bytes(encrypted), response=True)

    async def connect(self):
        if (self.authenticated and self.client and self.client.is_connected
                and time.monotonic() - self.connected_at < 18):
            return
        await self.close()
        # Reinitialize authorized crypto state, including ephemeral AES keys.
        self.crypt = self.crypt_type()
        generation = self.generation
        self.client = BleakClient(self.mac, timeout=25)
        sent_serial = False
        deadline = time.monotonic() + 60
        try:
            await self.client.connect()
            await self.client.start_notify(
                RX, lambda sender, data: self._notification(generation, sender, data)
            )
            for _ in range(25):
                remaining = min(15, deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError("BLE handshake deadline")
                packet = await asyncio.wait_for(self.queue.get(), remaining)
                response, status = self.crypt.ble_crypt_link_handler(packet)
                if response:
                    await self.client.write_gatt_char(TX, bytes(response), response=True)
                if status == 3 and not sent_serial:
                    await self._send(rtu(3, 17, 4))
                    sent_serial = True
                if status == 4:
                    self.authenticated = True
                    self.connected_at = time.monotonic()
                    LOG.info("Secure BLE authentication completed")
                    return
                if status not in (0, 1, 2, 3):
                    raise RuntimeError(f"Unexpected authentication status {status}")
            raise TimeoutError("BLE handshake incomplete")
        except BaseException:
            await self.close()
            raise

    async def _response(self, fc, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                encrypted = await asyncio.wait_for(self.queue.get(), deadline - time.monotonic())
            except asyncio.TimeoutError:
                return None
            try:
                packet = bytes(self.crypt.decrypt_data(encrypted))
            except (ValueError, TypeError) as exc:
                LOG.warning("Invalid encrypted notification: %s", exc)
                continue
            if len(packet) < 5 or packet[0] != 1:
                continue
            if crc16(packet[:-2]) != int.from_bytes(packet[-2:], "little"):
                LOG.warning("Invalid CRC on decrypted Modbus response")
                continue
            if packet[1] == (fc | 0x80):
                raise RuntimeError(f"Modbus exception {packet[2]}")
            if packet[1] == fc:
                return packet
        return None

    async def read(self, address, count=1):
        async with self.lock:
            await self.connect()
            while not self.queue.empty():
                self.queue.get_nowait()
            await self._send(rtu(3, address, count))
            packet = await self._response(3, 6)
            if packet is None:
                await self.close()
                raise TimeoutError(f"FC03 {address} timed out")
            if packet[2] != count * 2 or len(packet) != 5 + count * 2:
                await self.close()
                raise ValueError(f"Invalid FC03 reply for {address}")
            return struct.unpack(">" + "H" * count, packet[3:-2])

    async def write_and_verify(self, address, target, minimum, maximum):
        if not minimum <= target <= maximum:
            raise ValueError("Target outside configured range")
        old = (await self.read(address))[0]
        if old == target:
            return old
        async with self.lock:
            await self.connect()
            while not self.queue.empty():
                self.queue.get_nowait()
            await self._send(write16(address, target))
            try:
                ack = await self._response(16, 1.5)
                if ack and (len(ack) != 8
                            or int.from_bytes(ack[2:4], "big") != address
                            or int.from_bytes(ack[4:6], "big") != 1):
                    LOG.warning("FC16 unexpected ACK at %d", address)
            except Exception as exc:
                LOG.warning("FC16 acknowledgement unavailable: %s", exc)
        for attempt in range(3):
            await asyncio.sleep(1.2 if attempt == 0 else 0.8)
            try:
                actual = (await self.read(address))[0]
                if actual == target:
                    return actual
                LOG.warning("FC16 readback %d=%d; target=%d", address, actual, target)
            except Exception as exc:
                LOG.warning("FC16 readback error at %d: %s", address, exc)
        raise RuntimeError(f"FC16 {address} not verified; no automatic retry")
