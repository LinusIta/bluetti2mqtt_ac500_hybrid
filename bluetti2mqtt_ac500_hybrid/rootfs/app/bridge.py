#!/usr/bin/env python3
"""AC500 MQTT bridge: standalone application, BLE transport via pinned bluetti-bt-lib.

Stable 4.0.0: AC500 encrypted BLE/MQTT transport with B300 diagnostics.
Retains existing MQTT discovery identities for migrated entities.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import signal
from dataclasses import dataclass
from enum import Enum
from cryptography.exceptions import InvalidSignature
from typing import Any
from hybrid_modbus import AC500ModbusReader, MODBUS_FIELDS
from command_mailbox import CommandMailbox
from pack_scan_progress import PackScanProgress

import paho.mqtt.client as mqtt
from bluetti_bt_lib.devices.ac500 import AC500
from bluetti_bt_lib.bluetooth.device_reader import DeviceReader, DeviceReaderConfig
from bluetti_bt_lib.registers import ReadableRegisters, WriteableRegister
import time

LOG = logging.getLogger("bluetti2mqtt_plus")

# Explicit, narrow write policy. All addresses/values verified on user's AC500.
# Commands exposed by the legacy semitop7 MQTT discovery plus preview aliases.
# (register, minimum, maximum); deliberately exclude pack selector 3006,
# split-phase configuration and time-program blocks until separately validated.
WRITE_FIELDS = {
    "grid_charge_amps": (3019, 0, 25),
    "ups_mode": (3001, 1, 4),
    "ctrl_ups_mode": (3001, 1, 4),
    "auto_sleep_mode": (3061, 2, 5),
    "ctrl_display_timeout": (3061, 2, 5),
    "ac_output_on": (3007, 0, 1),
    "ctrl_ac": (3007, 0, 1),
    "dc_output_on": (3008, 0, 1),
    "ctrl_dc": (3008, 0, 1),
    "grid_charge_on": (3011, 0, 1),
    "time_control_on": (3013, 0, 1),
    "battery_range_start": (3015, 0, 100),
    "battery_range_end": (3016, 0, 100),
}
# These are published with original 3.0.3 names. Legacy retained Discovery
# may already be present; do not generate new duplicate control entities.
LEGACY_CONTROLS = {
    "ac_output_on", "dc_output_on", "ups_mode", "auto_sleep_mode",
    "grid_charge_on", "time_control_on", "battery_range_start",
    "battery_range_end", "grid_charge_amps",
}
BOOLEAN_WRITES = {"ac_output_on", "dc_output_on", "ctrl_ac", "ctrl_dc",
                  "grid_charge_on", "time_control_on"}
DISPLAY_VALUES = {"THIRTY_SECONDS": 2, "ONE_MINUTE": 3,
                  "FIVE_MINUTES": 4, "NEVER": 5,
                  "30 S": 2, "1 MIN": 3, "5 MIN": 4, "1 MINUTE": 3,
                  "5 MINUTES": 4}
DISPLAY_NAMES = {2: "THIRTY_SECONDS", 3: "ONE_MINUTE",
                 4: "FIVE_MINUTES", 5: "NEVER"}

NAMES = {
    "grid_charge_amps": "Grid Charge Amps",
    "ac_output_on": "AC Output",
    "dc_output_on": "DC Output",
    "ups_mode": "UPS Mode",
    "auto_sleep_mode": "Screen Auto Sleep Mode",
    "grid_charge_on": "Grid Charge",
    "time_control_on": "Time Control",
    "battery_range_start": "Battery Range Start",
    "battery_range_end": "Battery Range End",
    "ctrl_ups_mode": "UPS Mode",
    "ctrl_display_timeout": "Display Timeout",
    "ctrl_ac": "AC Output",
    "ctrl_dc": "DC Output",
    "total_battery_percent": "Battery SOC",
    "dc_input_power": "DC Input Power",
    "ac_input_power": "AC Input Power",
    "ac_output_power": "AC Output Power",
    "dc_output_power": "DC Output Power",
    "dc_input_voltage2": "DC Input Voltage 2",
    "dc_input_power2": "DC Input Power 2",
}
UNITS = {"grid_charge_amps": "A", "total_battery_percent": "%",
         "dc_input_power": "W", "ac_input_power": "W", "ac_output_power": "W",
         "dc_output_power": "W", "dc_input_voltage2": "V", "dc_input_power2": "W"}
# Existing MQTT entities may be used by Energy Dashboard/statistics. HA needs
# state_class (and device_class) in retained Discovery, not only the state value.
# Only numeric sensors receive these attributes; selects and switches never do.
SENSOR_METADATA = {
    "total_battery_percent": ("%", "battery", "measurement"),
    "dc_input_power": ("W", "power", "measurement"),
    "ac_input_power": ("W", "power", "measurement"),
    "ac_output_power": ("W", "power", "measurement"),
    "dc_output_power": ("W", "power", "measurement"),
    "dc_input_power1": ("W", "power", "measurement"),
    "dc_input_power2": ("W", "power", "measurement"),
    "dc_input_voltage1": ("V", "voltage", "measurement"),
    "dc_input_voltage2": ("V", "voltage", "measurement"),
    "dc_input_current1": ("A", "current", "measurement"),
    "total_battery_voltage": ("V", "voltage", "measurement"),
    "total_battery_current": ("A", "current", "measurement"),
    "internal_ac_voltage": ("V", "voltage", "measurement"),
    "internal_ac_frequency": ("Hz", "frequency", "measurement"),
    "ac_input_voltage": ("V", "voltage", "measurement"),
    "ac_input_frequency": ("Hz", "frequency", "measurement"),
    "pv_s1_voltage": ("V", "voltage", "measurement"),
    "pv_s1_current": ("A", "current", "measurement"),
    "pv_s1_power": ("W", "power", "measurement"),
    "power_generation": ("kWh", "energy", "total_increasing"),
    "internal_current_one": ("A", "current", "measurement"),
    "internal_current_two": ("A", "current", "measurement"),
    "internal_current_three": ("A", "current", "measurement"),
    "internal_power_one": ("W", "power", "measurement"),
    "internal_power_two": ("W", "power", "measurement"),
    "internal_power_three": ("W", "power", "measurement"),
    "internal_dc_input_voltage": ("V", "voltage", "measurement"),
    "internal_dc_input_power": ("W", "power", "measurement"),
    "internal_dc_input_current": ("A", "current", "measurement"),
}
# These Discovery IDs were introduced only by the standalone preview. Keep the
# original semitop-era IDs instead, so HA does not show two AC/DC switches.
PREVIEW_CONTROL_ALIASES = {
    "ctrl_ac": "switch",
    "ctrl_dc": "switch",
    "ctrl_ups_mode": "select",
    "ctrl_display_timeout": "select",
}

# Legacy AC500 field map, based on original AC500 Modbus layout.
# Each entry: name -> (address, decimal_places, unit, kind).
# Do not fabricate state for unavailable battery packs; see README.
LEGACY_CORE_FIELDS = {
    "power_generation": (41, 1, "kWh", "uint"),
    "internal_current_one": (72, 1, "A", "uint"),
    "internal_power_one": (73, 0, "W", "uint"),
    "internal_current_two": (75, 1, "A", "uint"),
    "internal_power_two": (76, 0, "W", "uint"),
    "internal_current_three": (78, 1, "A", "uint"),
    "internal_power_three": (79, 0, "W", "uint"),
    "internal_dc_input_voltage": (86, 1, "V", "uint"),
    "internal_dc_input_power": (87, 0, "W", "uint"),
    "internal_dc_input_current": (88, 1, "A", "uint"),
    "pack_num_max": (91, 0, None, "uint"),
    "total_battery_voltage": (92, 1, "V", "uint"),
    "split_phase_on": (3004, 0, None, "bool"),
    "split_phase_machine_mode": (3005, 0, None, "machine"),
    "grid_charge_on": (3011, 0, None, "bool"),
    "time_control_on": (3013, 0, None, "bool"),
    "battery_range_start": (3015, 0, "%", "uint"),
    "battery_range_end": (3016, 0, "%", "uint"),
}
# Field names preserved from the pre-4.0 MQTT publisher.
LEGACY_STATE_ALIASES = {
    "internal_ac_voltage": "internal_ac_voltage",
    "internal_ac_frequency": "internal_ac_frequency",
    "ac_output_mode": "ac_output_mode",
}

def legacy_register_value(raw: dict[int, bytes], address: int):
    """Extract a big-endian uint16 from any returned raw block covering address."""
    for start, body in raw.items():
        offset = (address - start) * 2
        if offset >= 0 and offset + 2 <= len(body):
            return int.from_bytes(body[offset:offset+2], "big")
    return None

def legacy_decode_field(field: str, value: int):
    addr, places, _, kind = LEGACY_CORE_FIELDS[field]
    if kind == "bool":
        return "ON" if value else "OFF"
    if kind == "machine":
        return {0: "SLAVE", 1: "MASTER"}.get(value, str(value))
    return value / (10 ** places) if places else value

UPS_OPTIONS = {"CUSTOMIZED": 1, "PV_PRIORITY": 2, "STANDARD": 3, "TIME_CONTROL": 4}
DISPLAY_OPTIONS = {"30 s": 2, "1 min": 3, "5 min": 4, "Never": 5}
# DisplayMode values are not all hardware-validated. Discovery limits are
# deliberately withheld for display until cross-checked on the user's model.

@dataclass(frozen=True)
class Settings:
    mac: str
    host: str
    port: int
    user: str
    password: str
    poll_sec: int
    serial: str
    ha_config: str
    allow_writes: bool
    pack_poll_sec: int
    pack_scan_sec: int
    modbus_host: str
    modbus_port: int
    modbus_poll_sec: int

    @classmethod
    def load(cls):
        data = json.load(open("/data/options.json", encoding="utf-8"))
        mac = str(data.get("bt_mac", "")).upper().strip()
        if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
            raise ValueError("Specify one valid AC500 bt_mac")
        serial = str(data.get("device_serial", "")).strip()
        if not re.fullmatch(r"[0-9]{5,20}", serial):
            raise ValueError("Set device_serial to AC500 serial from bluetti-read")
        return cls(
            mac=mac, host=str(data.get("mqtt_host") or os.getenv("BLUETTI_MQTT_HOST") or "core-mosquitto"),
            port=int(data.get("mqtt_port") or os.getenv("BLUETTI_MQTT_PORT") or 1883),
            user=str(data.get("mqtt_username") or os.getenv("BLUETTI_MQTT_USER") or ""),
            password=str(data.get("mqtt_password") or os.getenv("BLUETTI_MQTT_PASSWORD") or ""),
            poll_sec=max(15, int(data.get("poll_sec", 15))),
            serial=serial, ha_config=str(data.get("ha_config", "normal")),
            allow_writes=bool(data.get("allow_writes", False)),
            pack_poll_sec=max(90, int(data.get("pack_poll_sec", 600))),
            pack_scan_sec=max(600, int(data.get("pack_scan_sec", 3600))),
            modbus_host=str(data.get("modbus_host", "")).strip(),
            modbus_port=int(data.get("modbus_port", 502)),
            modbus_poll_sec=max(2, int(data.get("modbus_poll_sec", 5))),
        )

class OneWriteReader(DeviceReader):
    """Execute exactly one FC06 from a read session; check returned Modbus echo."""
    def __init__(self, *args, address: int, new_value: int, newer_command_waiting=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.address = address
        self.new_value = new_value
        self.response_ok = False
        self.previous_value = None
        self.verified_value = None
        self.newer_command_waiting = newer_command_waiting or (lambda: False)
        self.readback_attempts = 0
        self.readback_superseded = False

    async def _async_send_command(self, register):
        original = await super()._async_send_command(register)
        raw = register.parse_response(original)
        if len(raw) != 2:
            raise RuntimeError("Could not read pre-write register")
        self.previous_value = int.from_bytes(raw, "big")
        if self.previous_value == self.new_value:
            self.response_ok = True
            self.verified_value = self.previous_value
            return original
        write = WriteableRegister(self.address, self.new_value)
        reply = await super()._async_send_command(write)
        self.response_ok = bytes(reply) == bytes(write)
        if not self.response_ok:
            raise RuntimeError("FC06 echo mismatch or missing (write outcome unknown)")
        # AC500 often reports the *previous* value directly after FC06.
        # Make at most ONE immediate FC03 verification (no delay loops).
        # Any delayed update is reconciled by a scheduled normal BLE poll,
        # so this session can disconnect and release newer surplus commands.
        if self.newer_command_waiting():
            self.readback_superseded = True
        else:
            try:
                verify_register = ReadableRegisters(self.address, 1)
                verified = await super()._async_send_command(verify_register)
                data = verify_register.parse_response(verified)
                self.readback_attempts = 1
                if len(data) == 2:
                    self.verified_value = int.from_bytes(data, "big")
            except Exception as exc:
                LOG.warning("FC06 read-back unavailable for register %d: %s", self.address, exc)
        return original

# AC500 B300 pack selector and data block, verified against the actual AC500.
# 3006: selected slot; 96: reported slot; 98: voltage in centivolts;
# 99: SOC in percent; 105..120: 16 individual cell voltages in centivolts.
PACK_SELECTOR = 3006
PACK_START = 91
PACK_COUNT = 37
PACK_CELL_ADDRESSES = range(105, 121)
PACK_SETTLE_SECONDS = 10

class PackScanPreempted(Exception):
    """A queued MQTT command interrupted a noncritical B300 scan."""


class ChargeSessionReader(DeviceReader):
    """AC500 3019: reuse ONE encrypted session for rapid surplus updates.

    Hardware tested: repeated FC03 + FC06 on the same encrypted connection.
    End the session before the observed ~25s disconnect threshold. This class
    owns only register 3019; other controls keep the proven beta5 path.
    """

    SESSION_SECONDS = 21.0
    VERIFY_DELAY = 3.4
    VERIFY_RETRY = 1.0
    VERIFIED_IDLE_SECONDS = 2.0  # short reuse window after confirmed hardware state

    def __init__(self, *args, bridge, first_field, first_value, **kwargs):
        super().__init__(*args, **kwargs)
        self.bridge = bridge
        self.first_field = first_field
        self.first_value = first_value
        self.write_count = 0
        self.verified = 0
        self.first_ack = False

    async def _read_amps(self):
        reg = ReadableRegisters(3019, 1)
        reply = await super()._async_send_command(reg)
        body = reg.parse_response(reply)
        if len(body) != 2:
            raise RuntimeError("AC500 FC03 3019 returned no value")
        return int.from_bytes(body, "big")

    async def _async_send_command(self, register):
        # DeviceReader.read() already performed the ECDSA handshake, started
        # BLE notifications and will disconnect/reset keys after this returns.
        started = time.monotonic()
        deadline = started + self.SESSION_SECONDS
        initial_reply = await super()._async_send_command(register)
        initial_body = register.parse_response(initial_reply)
        if len(initial_body) != 2:
            raise RuntimeError("AC500 initial 3019 read failed")
        observed = int.from_bytes(initial_body, "big")
        self.bridge.last_registers[3019] = observed
        self.bridge.publish("grid_charge_amps", observed)
        self.bridge.commands.mark_verified(3019, observed, time.monotonic())
        field = self.first_field
        target = self.first_value
        verify_due = None
        verified_at = None

        async def issue_setpoint(new_field, new_value):
            nonlocal observed, field, target, verify_due, verified_at
            field, target = new_field, new_value
            verified_at = None
            # A read result is actual hardware evidence; never publish an
            # optimistic MQTT state on FC06 echo alone.
            if observed == target:
                self.bridge.commands.mark_verified(3019, target, time.monotonic())
                self.bridge.publish("grid_charge_amps", target)
                LOG.info("BLE session 3019 already at %d A", target)
                verify_due = None
                verified_at = time.monotonic()
                return
            write = WriteableRegister(3019, target)
            echo = await super(ChargeSessionReader, self)._async_send_command(write)
            if bytes(echo) != bytes(write):
                raise RuntimeError("AC500 FC06 echo missing/mismatched (outcome uncertain)")
            self.write_count += 1
            self.first_ack = True
            # Any prior hardware sample is now stale until the new target is
            # observed. Never deduplicate based on a pre-FC06 value.
            self.bridge.commands.verified.pop(3019, None)
            # The last FC03 sample predates FC06. It is NOT reliable evidence
            # for deduplicating a subsequent setpoint until we read again.
            observed = None
            verify_due = time.monotonic() + self.VERIFY_DELAY
            LOG.info("BLE session FC06 ACK: grid_charge_amps=%d A; "
                     "read-back scheduled (%.2fs into session)",
                     target, time.monotonic() - started)

        await issue_setpoint(field, target)
        LOG.info("BLE fast session ready: duration limit %.0fs, register 3019",
                 self.SESSION_SECONDS)

        while not self.bridge.stop_event.is_set():
            remaining = deadline - time.monotonic()
            # Leave time for cleanup before the observed BLE disconnection.
            if remaining <= 1.0 or not self.client or not self.client.is_connected:
                break

            # Other control registers (SOC, UPS, etc.) take priority over
            # keeping the 3019 session alive. Never consume their commands.
            if any(address != 3019 for address in self.bridge.commands.pending):
                LOG.info("BLE fast session yielding to another control register")
                break

            if 3019 in self.bridge.commands.pending:
                # The mailbox already collapsed rapid changes to the latest
                # desired value. Keep active bookkeeping aligned with the
                # command actually being executed.
                self.bridge.commands.finish(3019, target)
                next_command = self.bridge.commands.pop(time.monotonic())
                if next_command:
                    _, new_field, payload, new_value, received_at = next_command
                    LOG.info("MQTT grid_charge_amps=%d reused encrypted BLE "
                             "session after %.2fs queued",
                             new_value, time.monotonic() - received_at)
                    if new_value != target:
                        await issue_setpoint(new_field, new_value)
                    else:
                        # Identical fresh request: the existing FC06 ACK still
                        # needs a real hardware read-back, not a second FC06.
                        field = new_field
                continue

            if verify_due is not None and time.monotonic() >= verify_due:
                # Read-back costs ~0.1s in a live encrypted session. It also
                # updates the HA number promptly, unlike a new 10s handshake.
                observed = await self._read_amps()
                self.bridge.last_registers[3019] = observed
                self.bridge.publish("grid_charge_amps", observed)
                if observed == target:
                    self.bridge.commands.mark_verified(3019, observed, time.monotonic())
                LOG.info("BLE fast session hardware grid_charge_amps=%d A "
                         "(requested=%d A, +%.1fs)", observed, target,
                         time.monotonic() - started)
                if observed == target:
                    self.verified += 1
                    verify_due = None
                    verified_at = time.monotonic()
                else:
                    verify_due = time.monotonic() + self.VERIFY_RETRY
                continue

            # Release BlueZ as soon as the last requested value is verified,
            # but permit another fresh setpoint during this short grace period.
            # Pending requests are always processed BEFORE the idle check.
            if (verify_due is None and verified_at is not None
                    and time.monotonic() - verified_at >= self.VERIFIED_IDLE_SECONDS):
                LOG.info("BLE fast session closing early: verified idle %.1fs",
                         time.monotonic() - verified_at)
                break

            await asyncio.sleep(min(0.15, max(0.01, remaining - 1.0)))

        LOG.info("BLE fast session complete: %.1fs, writes=%d, verified=%d, "
                 "last_target=%d, last_observed=%s",
                 time.monotonic() - started, self.write_count, self.verified,
                 target, observed)
        # A normal poll can resolve a late hardware application. Do not retry
        # FC06 automatically because a newer command might already supersede it.
        if verify_due is not None:
            self.bridge.refresh_requested.set()
        self.bridge.commands.finish(3019, target)
        return initial_reply


class PackPollingReader(DeviceReader):
    """Scan selected B300 slots over ONE encrypted BLE session.

    Implemented through an initial selector FC03 in DeviceReader.read(), so
    all subsequent selector FC06 and pack FC03 requests reuse the verified
    encrypted transport.  Pack writes are operational selection only, never
    energy configuration writes.  The selector is restored in finally.
    """

    def __init__(self, *args, targets: list[int], priority_event: asyncio.Event, **kwargs):
        super().__init__(*args, **kwargs)
        self.targets = list(targets)
        self.priority_event = priority_event
        self.was_preempted = False
        self.packs: dict[int, dict[str, Any]] = {}
        self.empty_slots: set[int] = set()
        self.failed_slots: set[int] = set()
        self.restore_ok = True

    @staticmethod
    def _word(data: bytes, address: int) -> int:
        offset = (address - PACK_START) * 2
        return int.from_bytes(data[offset:offset+2], "big")

    async def _async_send_command(self, register):
        original_reply = await super()._async_send_command(register)
        original_bytes = register.parse_response(original_reply)
        if len(original_bytes) != 2:
            raise RuntimeError("Could not read original B300 selector")
        original_slot = int.from_bytes(original_bytes, "big")
        active_slot = original_slot
        LOG.info("B300 scan targets=%s; selector initially %s", self.targets, original_slot)

        try:
            for slot in self.targets:
                try:
                    if self.priority_event.is_set():
                        raise PackScanPreempted()
                    if active_slot != slot:
                        selector_write = WriteableRegister(PACK_SELECTOR, slot)
                        echo = await super()._async_send_command(selector_write)
                        if bytes(echo) != bytes(selector_write):
                            raise RuntimeError(f"B300 slot {slot} selector echo mismatch")
                        active_slot = slot

                    # Device refreshes the multiplexed BMS block asynchronously.
                    try:
                        await asyncio.wait_for(self.priority_event.wait(), timeout=PACK_SETTLE_SECONDS)
                        raise PackScanPreempted()
                    except asyncio.TimeoutError:
                        pass
                    request = ReadableRegisters(PACK_START, PACK_COUNT)
                    response = await super()._async_send_command(request)
                    block = request.parse_response(response)
                    if len(block) != PACK_COUNT * 2:
                        raise RuntimeError(f"B300 slot {slot}: incomplete block ({len(block)} bytes)")
                    reported_slot = self._word(block, 96)
                    pack_voltage = self._word(block, 98) / 100
                    percent = self._word(block, 99)
                    voltages = [self._word(block, a) / 100 for a in PACK_CELL_ADDRESSES]
                    if reported_slot != slot:
                        raise RuntimeError(f"B300 slot {slot}: device reported slot {reported_slot}")
                    # Unpopulated slots return 0V/0%; do not mistake a real
                    # fully discharged pack (0% but positive voltage) for empty.
                    if pack_voltage == 0 and all(v == 0 for v in voltages):
                        self.empty_slots.add(slot)
                        LOG.info("B300 slot %d: unpopulated", slot)
                        continue
                    if not (30.0 <= pack_voltage <= 65.0 and 0 <= percent <= 100
                            and len(voltages) == 16
                            and all(2.0 <= v <= 4.3 for v in voltages)):
                        raise RuntimeError(
                            f"B300 slot {slot}: implausible voltage/SOC/cell data "
                            f"({pack_voltage}V, {percent}%%)"
                        )
                    self.packs[slot] = {
                        "percent": percent,
                        "voltage": pack_voltage,
                        "voltages": voltages,
                    }
                    LOG.info("B300 slot %d: %.2f V, %d%%, 16 cells", slot, pack_voltage, percent)
                except PackScanPreempted:
                    self.was_preempted = True
                    LOG.info("B300 slot %d paused to prioritize MQTT command", slot)
                    # This is expected scheduling, not a BLE fault. Returning
                    # after finally restores the selector without causing
                    # DeviceReader to emit a misleading "Unknown error".
                    break
                except Exception as exc:
                    self.failed_slots.add(slot)
                    LOG.warning("B300 slot %d scan failed: %s", slot, exc)
                    # Avoid further commands on an uncertain/failed BLE session.
                    break
        finally:
            if active_slot != original_slot and 1 <= original_slot <= 6:
                try:
                    restore = WriteableRegister(PACK_SELECTOR, original_slot)
                    reply = await super()._async_send_command(restore)
                    self.restore_ok = bytes(reply) == bytes(restore)
                    if not self.restore_ok:
                        LOG.error("B300 selector restore FC06 echo mismatch")
                except Exception:
                    self.restore_ok = False
                    LOG.exception("B300 selector restore failed")
            elif not (1 <= original_slot <= 6):
                LOG.warning("B300 original selector=%s is unknown: not restoring", original_slot)
        return original_reply

class Bridge:
    def __init__(self, opts: Settings):
        self.opts = opts
        self.device = AC500()
        self.device.max_packs = 0  # preview: disable battery pack selector writes
        self.commands = CommandMailbox(max_age=45.0, verified_ttl=30.0)
        self.pending_write = asyncio.Event()
        self.crypto_faults = 0
        self.loop = asyncio.get_running_loop()
        self.lock = asyncio.Lock()  # DeviceReader internal request lock
        # A separate lock must cover the ENTIRE BLE connection/session.
        # The library request lock does not serialize separate reader instances.
        self.ble_session_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.last_registers: dict[int, int] = {}
        self.active_pack_slots: set[int] = set()
        self.last_full_pack_scan = 0.0
        self.pack_scan: PackScanProgress | None = None
        self.pack_resume_delay = None
        self.modbus_last_ok = 0.0
        self.modbus_publishing = False
        self.modbus_recent_fields = set()
        self.modbus_failure_logged = False
        self.refresh_requested = asyncio.Event()
        self.prefix = f"bluetti/state/AC500-{opts.serial}/"
        self.command_prefix = f"bluetti/command/AC500-{opts.serial}/"
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"bluetti-plus-{opts.serial}")
        if opts.user:
            self.mqtt.username_pw_set(opts.user, opts.password)
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_message = self.on_message
        self.mqtt.on_disconnect = self.on_disconnect

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.error("MQTT connection rejected: %s", reason_code)
            return
        LOG.info("MQTT connected; commands %s", self.command_prefix)
        client.subscribe(self.command_prefix + "+", qos=1)
        self.loop.call_soon_threadsafe(self.publish_discovery)

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        LOG.warning("MQTT disconnected: %s", reason_code)

    def on_message(self, client, userdata, msg):
        if not msg.topic.startswith(self.command_prefix):
            return
        field = msg.topic[len(self.command_prefix):]
        # A retained command is historical broker state, not a new user action.
        # Executing it at every reconnect may unexpectedly alter AC500 settings.
        if msg.retain:
            LOG.warning("Ignored retained MQTT command on %s", msg.topic)
            return
        try:
            value = msg.payload.decode("utf-8").strip()
        except UnicodeDecodeError:
            return
        self.loop.call_soon_threadsafe(self.enqueue_command, field, value)

    def enqueue_command(self, field: str, value: str):
        # Last desired value wins per physical register (alias-aware).
        # No BLE work is scheduled for duplicate in-flight/verified values.
        try:
            address, numeric = self.parse_write(field, value)
        except (KeyError, ValueError) as exc:
            LOG.warning("Rejected MQTT command %r=%r: %s", field, value, exc)
            return
        outcome = self.commands.put(address, field, value, numeric, time.monotonic())
        if outcome in ("queued", "replaced"):
            self.pending_write.set()
        if outcome == "replaced":
            LOG.info("Updated pending MQTT setpoint %s=%s (previous value discarded)", field, value)
        elif outcome == "already_active":
            LOG.debug("MQTT setpoint %s=%s already being applied", field, value)
        elif outcome == "recently_verified":
            LOG.debug("MQTT setpoint %s=%s already verified recently", field, value)

    async def wait_for_pending_writes(self):
        # Between battery probes, give the command worker first access to BLE.
        # Keep a full pack scan resumable instead of monopolizing BLE for ~1min.
        while self.pending_write.is_set() and not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    def publish(self, name, value, retain=True):
        # Modbus is authoritative only while healthy; BLE remains fallback.
        if (not self.modbus_publishing and name in self.modbus_recent_fields
                and self.modbus_last_ok
                and time.monotonic() - self.modbus_last_ok < max(15, self.opts.modbus_poll_sec * 3)):
            return
        if isinstance(value, Enum):
            value = value.name
        elif isinstance(value, bool):
            value = "ON" if value else "OFF"
        elif hasattr(value, "value") and hasattr(value, "unit"):
            value = value.value
        self.mqtt.publish(self.prefix + name, str(value), qos=1, retain=retain)

    def publish_discovery(self):
        if self.opts.ha_config == "none":
            return
        device = {"identifiers": [self.opts.serial], "manufacturer": "Bluetti",
                  "name": f"AC500 {self.opts.serial}", "model": "AC500"}
        # Clear only preview-created alias Discovery configs. These are retained
        # MQTT messages, so stopping an old version does not remove them.
        for alias, domain in PREVIEW_CONTROL_ALIASES.items():
            topic = f"homeassistant/{domain}/{self.opts.serial}_{alias}/config"
            self.mqtt.publish(topic, payload=b"", qos=1, retain=True)
            LOG.debug("Cleared obsolete preview alias Discovery: %s", topic)
        # Dev7 accidentally introduced fresh pack unique IDs alongside the
        # original semitop-era pack_percentN/pack_voltageN entities. Delete ONLY
        # those dev7-only retained configs; preserve the legacy Discovery.
        for slot in range(1, 7):
            for suffix in ("percent", "voltage"):
                field = f"battery_pack_{slot}_{suffix}"
                topic = f"homeassistant/sensor/{self.opts.serial}_{field}/config"
                self.mqtt.publish(topic, payload=b"", qos=1, retain=True)
                LOG.debug("Cleared obsolete dev7-only B300 Discovery: %s", topic)
        fields = ["total_battery_percent", "dc_input_power", "ac_input_power",
                  "ac_output_power", "dc_output_power", "dc_input_voltage2", "dc_input_power2",
                  "grid_charge_amps",
                  "ac_output_mode", "internal_ac_voltage", "internal_ac_frequency",
                  "ac_input_voltage", "ac_input_frequency", "dc_input_voltage1",
                  "dc_input_power1", "dc_input_current1", "total_battery_current"] + list(LEGACY_CORE_FIELDS)
        fields = list(dict.fromkeys(fields + ["ac_output_on", "dc_output_on",
            "ups_mode", "auto_sleep_mode", "grid_charge_on", "time_control_on"]))
        for field in fields:
            if field in PREVIEW_CONTROL_ALIASES:
                continue  # Never recreate the duplicate control entity.
            if field in ("grid_charge_amps", "battery_range_start", "battery_range_end"):
                domain = "number"
            elif field in ("ctrl_ac", "ctrl_dc", "ac_output_on", "dc_output_on",
                           "grid_charge_on", "time_control_on"):
                domain = "switch"
            elif field in ("ctrl_ups_mode", "ctrl_display_timeout", "ups_mode", "auto_sleep_mode"):
                domain = "select"
            else:
                domain = "sensor"
            conf = {"name": NAMES.get(field, field), "unique_id": f"{self.opts.serial}_{field}",
                    "object_id": f"AC500_{field}", "device": device,
                    "state_topic": self.prefix + field}
            if domain == "sensor" and field in SENSOR_METADATA:
                unit, device_class, state_class = SENSOR_METADATA[field]
                conf.update(unit_of_measurement=unit, device_class=device_class,
                            state_class=state_class)
            elif field in UNITS:
                conf["unit_of_measurement"] = UNITS[field]
            if domain == "number":
                conf.update(min=0, max=25 if field == "grid_charge_amps" else 100,
                            step=1, mode="box")
                if field in ("battery_range_start", "battery_range_end"):
                    conf["unit_of_measurement"] = "%"
            if field in ("ctrl_ups_mode", "ups_mode"):
                conf["options"] = list(UPS_OPTIONS)
            if field == "auto_sleep_mode":
                conf["options"] = list(DISPLAY_NAMES.values())
            if domain in ("number", "switch", "select"):
                conf["command_topic"] = self.command_prefix + field
            topic = f"homeassistant/{domain}/{self.opts.serial}_{field}/config"
            self.mqtt.publish(topic, json.dumps(conf), qos=1, retain=True)

    def publish_pack_discovery(self, slot: int):
        """Restore the original 3.0.3 MQTT Discovery IDs and JSON state topics.

        Existing MQTT/YAML cell sensors listen on pack_detailsN. Do not create
        a second set of pack aggregate entities with new unique IDs.
        """
        if self.opts.ha_config == "none":
            return
        device = {"identifiers": [self.opts.serial], "manufacturer": "Bluetti",
                  "name": f"AC500 {self.opts.serial}", "model": "AC500"}
        for suffix, legacy_id, unit, device_class, template in (
            ("percent", f"pack_percent{slot}", "%", "battery", "{{ value_json.percent }}"),
            ("voltage", f"pack_voltage{slot}", "V", "voltage", "{{ value_json.voltage }}"),
        ):
            payload = {"name": f"Battery Pack {slot} {suffix.title()}",
                       "unique_id": f"{self.opts.serial}_{legacy_id}",
                       "object_id": f"AC500_{legacy_id}", "device": device,
                       "state_topic": self.prefix + f"pack_details{slot}",
                       "value_template": template,
                       "unit_of_measurement": unit,
                       "device_class": device_class,
                       "state_class": "measurement"}
            topic = f"homeassistant/sensor/{self.opts.serial}_{legacy_id}/config"
            self.mqtt.publish(topic, json.dumps(payload), qos=1, retain=True)

    def publish_pack_sample(self, slot: int, details: dict[str, Any]):
        """Publish only validated hardware data, never synthetic zero/unknown."""
        self.publish_pack_discovery(slot)
        self.publish(f"battery_pack_{slot}_percent", details["percent"])
        self.publish(f"battery_pack_{slot}_voltage", details["voltage"])
        self.mqtt.publish(self.prefix + f"pack_details{slot}",
                          json.dumps(details, separators=(",", ":")),
                          qos=1, retain=True)

    async def poll_packs_once(self, full_scan: bool):
        # Resume the unfinished scan first; do not restart from slot 1 each
        # time the surplus automation preempts us. New cycles begin only after
        # the previous one has genuinely completed.
        if self.pack_scan is None:
            slots = (list(range(1, 7)) if full_scan or not self.active_pack_slots
                     else sorted(self.active_pack_slots))
            kind = "full" if full_scan or not self.active_pack_slots else "active"
            self.pack_scan = PackScanProgress.begin(kind, slots)
            LOG.info("B300 %s scan started: targets=%s", kind, slots)
        else:
            LOG.info("B300 %s scan resuming: pending=%s acquired=%s empty=%s",
                     self.pack_scan.kind, self.pack_scan.remaining,
                     sorted(self.pack_scan.acquired), sorted(self.pack_scan.empty))

        progress = self.pack_scan
        status = "preempted"  # Includes stop_event, unless fully completed.
        # A failed scan remains resumable too, but back off to the normal
        # interval instead of reconnecting continuously on a broken radio.
        for slot in list(progress.remaining):
            if self.stop_event.is_set():
                break

            reader = self.new_reader(PackPollingReader, targets=[slot],
                                     priority_event=self.pending_write)
            try:
                # Check both before and *after* taking the BLE session lock.
                # Only the command worker may clear pending_write.
                await self.wait_for_pending_writes()
                if self.stop_event.is_set():
                    break
                async with self.ble_session_lock:
                    if self.pending_write.is_set():
                        LOG.info("B300 slot %d preempted before BLE connection", slot)
                        break
                    try:
                        response = await reader.read(
                            only_registers=[ReadableRegisters(PACK_SELECTOR, 1)],
                            raw=True)
                    finally:
                        # Existing BlueZ handshakes require a short quiet gap.
                        await asyncio.sleep(2)

                # bluetti-bt-lib swallows callback exceptions and can return
                # None even when our reader marked itself as preempted.
                if reader.was_preempted:
                    LOG.info("B300 slot %d preempted for MQTT; preserving previous samples", slot)
                    break

                if not reader.restore_ok:
                    raise RuntimeError(f"B300 slot {slot} selector restore unconfirmed")
                if reader.packs.get(slot) is not None:
                    details = reader.packs[slot]
                    progress.record(slot, populated=True)
                    self.active_pack_slots.add(slot)
                    self.publish_pack_sample(slot, details)
                elif slot in reader.empty_slots:
                    progress.record(slot, populated=False)
                    self.active_pack_slots.discard(slot)
                    # Do not overwrite any retained HA values with fabricated
                    # zero/unknown data. A proven empty slot is not published.
                else:
                    raise RuntimeError(
                        f"B300 slot {slot}: no validated sample "
                        f"(read result={'none' if response is None else 'incomplete'})")
            except Exception as exc:
                progress.failed(slot)
                LOG.warning("B300 slot %d scan failed, will retry later: %s", slot, exc)
                status = "partial"
                break

            # Yield between isolated sessions. Never monopolise BlueZ while
            # the charge automation is issuing fresh MQTT setpoints.
            await asyncio.sleep(3)
            if self.pending_write.is_set():
                LOG.info("B300 scan paused between slots for queued MQTT command")
                break

        if progress.complete:
            status = "complete"
            if progress.kind == "full":
                self.last_full_pack_scan = time.monotonic()
            LOG.info("B300 %s scan complete: acquired=%s empty=%s known=%s failures=%s",
                     progress.kind, sorted(progress.acquired),
                     sorted(progress.empty), sorted(self.active_pack_slots),
                     sorted(progress.failures))
            self.pack_scan = None
            self.pack_resume_delay = self.opts.pack_poll_sec
        else:
            # Failures must not be marked complete. An interruption does not
            # reset the full-scan clock or lose previously acquired slots.
            if status == "partial":
                self.pack_resume_delay = self.opts.pack_poll_sec
            else:
                status = "preempted"
                self.pack_resume_delay = 15
            LOG.info("B300 %s scan %s: pending=%s acquired=%s empty=%s "
                     "failures=%s; resume in %ss",
                     progress.kind, status, progress.remaining,
                     sorted(progress.acquired), sorted(progress.empty),
                     sorted(progress.failures), self.pack_resume_delay)

    async def pack_polling(self):
        # Let main AC500 polling establish the first encrypted session.
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=120)
            return
        except asyncio.TimeoutError:
            pass
        while not self.stop_event.is_set():
            full_scan = (time.monotonic() - self.last_full_pack_scan >= self.opts.pack_scan_sec)
            try:
                await self.poll_packs_once(full_scan)
            except Exception:
                LOG.exception("B300 pack polling error (regular polling continues)")
            delay = self.pack_resume_delay or self.opts.pack_poll_sec
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def new_reader(self, cls=DeviceReader, **kwargs):
        return cls(self.opts.mac, self.device,
                   lambda: asyncio.get_running_loop().create_future(),
                   config=DeviceReaderConfig(timeout=60, use_encryption=True),
                   lock=self.lock, **kwargs)

    async def poll_once(self):
        # Raw reading allows use of new fields absent from bluetti-bt-lib's AC500 map.
        regs = list(self.device.get_polling_registers())
        seen = {r.starting_address for r in regs}
        # Ensure legacy regions are read even when library v0.1.8 omits them.
        # Each range must be complete before a legacy field is published.
        for address, count in ((10, 40), (70, 21), (91, 2),
                               (170, 3), (3001, 20), (3019, 1), (3061, 1)):
            # Existing reads must fully cover the requested interval.
            covered = address in seen
            if not covered:
                regs.append(ReadableRegisters(address, count))
        # The AC500 profile contains short polling blocks starting at some
        # of these regions. Checking only starting_address (above) does not
        # guarantee coverage of their later registers. Read these missing
        # legacy addresses explicitly, without changing any device setting.
        # Different starts avoid collisions with existing response keys.
        legacy_required = (
            (41, 1),        # Power generation (raw register)
            (72, 8),        # Three internal currents/powers: 72..79
            (3011, 3),     # Grid charge ON and time control ON
        )
        existing_starts = {r.starting_address for r in regs}
        for start, count in legacy_required:
            if start not in existing_starts:
                regs.append(ReadableRegisters(start, count))
                existing_starts.add(start)

        async with self.ble_session_lock:
            try:
                raw = await self.new_reader().read(only_registers=regs, raw=True)
            finally:
                # Give BlueZ and pending notification callbacks time to settle
                # before another connection can begin its encrypted handshake.
                await asyncio.sleep(2)
        if not raw:
            raise RuntimeError("BLE poll returned no registers")
        # Log the actual hardware readback, not the last MQTT setpoint. The
        # user's surplus controller depends on timely, truthful state updates.
        actual_amps = legacy_register_value(raw, 3019)
        if actual_amps is not None:
            self.last_registers[3019] = actual_amps
            self.commands.mark_verified(3019, actual_amps, time.monotonic())
            LOG.info("BLE hardware grid_charge_amps=%d A (register 3019)", actual_amps)
        else:
            LOG.warning("BLE poll did not return register 3019; cannot verify charge amps")
        parsed: dict[str, Any] = {}
        for address, body in raw.items():
            try:
                values = self.device.parse(address, body)
            except Exception:
                values = {}
            for key, value in values.items():
                k = key.value if hasattr(key, "value") else str(key)
                parsed[str(k)] = value
        # Publish original 3.0.3 entity names from the Modbus map.
        for field, (addr, places, unit, kind) in LEGACY_CORE_FIELDS.items():
            v = legacy_register_value(raw, addr)
            if v is not None:
                self.last_registers[addr] = v
                self.publish(field, legacy_decode_field(field, v))
        # Legacy command names must receive matching state updates, otherwise
        # retained Home Assistant discovery shows 'unknown' after migration.
        legacy_control_registers = {
            "ups_mode": 3001, "ac_output_on": 3007, "dc_output_on": 3008,
            "grid_charge_on": 3011, "time_control_on": 3013,
            "auto_sleep_mode": 3061,
        }
        for field, address in legacy_control_registers.items():
            v = legacy_register_value(raw, address)
            if v is None:
                continue
            self.last_registers[address] = v
            if field == "ups_mode":
                value = next((name for name, code in UPS_OPTIONS.items() if code == v), str(v))
            elif field == "auto_sleep_mode":
                value = DISPLAY_NAMES.get(v, str(v))
            else:
                value = "ON" if v else "OFF"
            self.publish(field, value)
        # Preserve additional MQTT topics injected by the original 3.0.3 Dockerfile.
        legacy_aliases = {
            "internal_dc_input_voltage": "dc_input_voltage1",
            "internal_dc_input_power": "dc_input_power1",
            "internal_dc_input_current": "dc_input_current1",
        }
        for original, alias in legacy_aliases.items():
            v = legacy_register_value(raw, LEGACY_CORE_FIELDS[original][0])
            if v is not None:
                self.publish(alias, legacy_decode_field(original, v))
        aliases = {"battery_soc": "total_battery_percent"}
        for field, value in parsed.items():
            self.publish(field, value)
            if field in aliases:
                self.publish(aliases[field], value)
        if 3019 in raw and len(raw[3019]) >= 2:
            self.publish("grid_charge_amps", int.from_bytes(raw[3019][:2], "big"))
        if 170 in raw and len(raw[170]) >= 6:
            self.publish("dc_input_voltage2", int.from_bytes(raw[170][:2], "big") / 10)
            self.publish("dc_input_power2", int.from_bytes(raw[170][4:6], "big"))
        if 3061 in raw and len(raw[3061]) >= 2:
            val = int.from_bytes(raw[3061][:2], "big")
            self.publish("ctrl_display_timeout", {3: "1 min", 4: "5 min"}.get(val, val))
        LOG.info("BLE poll successful: %d register groups, %d parsed values", len(raw), len(parsed))

    def parse_write(self, field, text):
        if field not in WRITE_FIELDS:
            raise ValueError("Unsupported field")
        val = text.strip()
        upper = val.upper()
        if field in BOOLEAN_WRITES:
            choices = {"ON": 1, "OFF": 0, "TRUE": 1, "FALSE": 0, "1": 1, "0": 0}
            if upper not in choices:
                raise ValueError("Expected ON/OFF or 1/0")
            value = choices[upper]
        elif field in ("ctrl_ups_mode", "ups_mode"):
            value = UPS_OPTIONS[upper] if upper in UPS_OPTIONS else int(val)
        elif field in ("ctrl_display_timeout", "auto_sleep_mode"):
            value = DISPLAY_VALUES[upper] if upper in DISPLAY_VALUES else int(val)
        else:
            value = int(val)
        address, lo, hi = WRITE_FIELDS[field]
        if not lo <= value <= hi:
            raise ValueError(f"Out of permitted range {lo}..{hi}")
        if field == "battery_range_start":
            end = self.last_registers.get(3016)
            if end is not None and value > end:
                raise ValueError(f"SOC start cannot exceed current SOC end ({end})")
        if field == "battery_range_end":
            start = self.last_registers.get(3015)
            if start is not None and value < start:
                raise ValueError(f"SOC end cannot be below current SOC start ({start})")
        return address, value

    async def handle_write(self, field, payload):
        if not self.opts.allow_writes:
            LOG.warning("Write blocked (allow_writes=false): %s", field)
            return
        try:
            address, value = self.parse_write(field, payload)
        except (KeyError, ValueError) as ex:
            LOG.error("Rejected command %r=%r: %s", field, payload, ex)
            return
        LOG.info("FC06 %s register=%d value=%d", field, address, value)
        if address == 3019:
            # Experimental fast path: one ECDSA handshake, many commands and
            # read-backs. Other registers retain the beta5 isolated writer.
            reader = self.new_reader(ChargeSessionReader, bridge=self,
                                     first_field=field, first_value=value)
            async with self.ble_session_lock:
                try:
                    outcome = await reader.read(
                        only_registers=[ReadableRegisters(3019, 1)], raw=True)
                finally:
                    await asyncio.sleep(2)
            if outcome is None:
                LOG.error("BLE fast session failed; do not replay uncertain FC06")
            return
        started = time.monotonic()
        reader = self.new_reader(OneWriteReader, address=address, new_value=value,
                                 newer_command_waiting=lambda: self.commands.has_pending)
        async with self.ble_session_lock:
            try:
                result = await reader.read(only_registers=[ReadableRegisters(address, 1)], raw=True)
            finally:
                await asyncio.sleep(2)
        if result is None or not reader.response_ok:
            LOG.error("Write unconfirmed: %s=%s", field, payload)
            return
        if reader.verified_value == value:
            self.last_registers[address] = value
            # This has been read back from hardware, not optimistically
            # mirrored from the MQTT command.
            self.publish(field, value)
            self.commands.mark_verified(address, value, time.monotonic())
            LOG.info("FC06 verified: %s=%s (%.1fs from writer start)",
                     field, payload, time.monotonic() - started)
        else:
            # FC06 echo is transport acknowledgement, not proof that the
            # firmware has already applied the new value. An older target may
            # legitimately appear during the asynchronous register update.
            LOG.warning("FC06 acknowledged, hardware read-back pending: "
                        "%s target=%s observed=%s attempts=%d superseded=%s",
                        field, payload, reader.verified_value,
                        reader.readback_attempts, reader.readback_superseded)
            if not reader.readback_superseded:
                self.refresh_requested.set()
        # Do not retry the write automatically: a duplicate or old command can
        # override a newer operator request. Next BLE polling reconciles state.

    async def worker(self):
        while not self.stop_event.is_set():
            await self.pending_write.wait()
            if self.stop_event.is_set():
                break
            command = self.commands.pop(time.monotonic())
            if command is None:
                self.pending_write.clear()
                continue
            address, field, payload, numeric, received_at = command
            try:
                elapsed = time.monotonic() - received_at
                if elapsed > self.commands.max_age:
                    LOG.warning("Dropped stale MQTT setpoint %s=%s (age %.1fs); "
                                "waiting for a fresh command", field, payload, elapsed)
                    continue
                LOG.info("MQTT command %s dispatched after %.2fs in queue",
                         field, elapsed)
                await self.handle_write(field, payload)
            except Exception:
                LOG.exception("Command failed: %s", field)
            finally:
                self.commands.finish(address, numeric)
                if not self.commands.has_pending:
                    self.pending_write.clear()

    async def modbus_polling(self):
        """Independent Modbus TCP telemetrics, no Modbus writes."""
        if not self.opts.modbus_host:
            LOG.warning("Modbus host not configured: BLE fallback telemetry only")
            return
        reader = AC500ModbusReader(self.opts.modbus_host, self.opts.modbus_port)
        LOG.info("Modbus TCP reader starting: %s:%d (unit 1)",
                 self.opts.modbus_host, self.opts.modbus_port)
        try:
            while not self.stop_event.is_set():
                cycle_start = time.monotonic()
                try:
                    data = await reader.read_fields()
                    if not data or not 0 <= data["total_battery_percent"] <= 100:
                        raise RuntimeError("Modbus returned implausible battery SOC")
                    recovered = self.modbus_failure_logged or not self.modbus_last_ok
                    self.modbus_last_ok = time.monotonic()
                    self.modbus_recent_fields = set(data)
                    self.modbus_failure_logged = False
                    self.modbus_publishing = True
                    try:
                        for field, value in data.items():
                            self.publish(field, value)
                    finally:
                        self.modbus_publishing = False
                    if recovered:
                        LOG.info("Modbus TCP reading active: %d fields; BLE fallback retained", len(data))
                except Exception as exc:
                    if not self.modbus_failure_logged:
                        LOG.warning("Modbus TCP unavailable (%s); BLE polling acts as fallback", exc)
                        self.modbus_failure_logged = True
                    await reader.close()
                delay = max(0.2, self.opts.modbus_poll_sec - (time.monotonic() - cycle_start))
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            await reader.close()

    async def polling(self):
        # poll_sec is start-to-start, rather than an idle delay added after
        # every BLE transaction.  An overlong transaction simply shifts the
        # next poll; never overlap encrypted BLE sessions.
        while not self.stop_event.is_set():
            cycle_start = time.monotonic()
            failed = False
            try:
                # Keep an operator command ahead of a periodic read.
                await self.wait_for_pending_writes()
                if self.stop_event.is_set():
                    break
                self.refresh_requested.clear()
                await self.poll_once()
            except Exception:
                failed = True
                LOG.exception("BLE polling error")
            elapsed = time.monotonic() - cycle_start
            # After an error allow a small recovery interval, not a tight
            # loop of repeated BLE handshakes.
            # Fail back to the old ~15-second BLE cadence while Modbus is down.
            modbus_ok = bool(self.modbus_last_ok and time.monotonic() - self.modbus_last_ok < max(15, self.opts.modbus_poll_sec * 3))
            interval = self.opts.poll_sec if modbus_ok else 15
            delay = max(2.0 if failed else 0.0, interval - elapsed)
            # Wake a periodic BLE reader promptly for command read-back;
            # keep commands prioritised by the existing pending_write gate.
            end = time.monotonic() + delay
            while not self.stop_event.is_set() and not self.refresh_requested.is_set():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=min(2.0, remaining))
                except asyncio.TimeoutError:
                    pass

    def handle_asyncio_exception(self, loop, context):
        # bluetti-bt-lib launches BLE notification callbacks as independent
        # asyncio tasks. Their InvalidSignature exceptions can otherwise
        # appear as 'Task exception was never retrieved'. They do NOT prove
        # the current command/read was successful; its own timeout still
        # applies, and DeviceReader creates a new session on next attempt.
        exc = context.get("exception")
        if isinstance(exc, InvalidSignature):
            self.crypto_faults += 1
            LOG.warning("Encrypted BLE handshake rejected an ECDSA signature "
                        "(count=%d); current session must finish/fail before reconnect",
                        self.crypto_faults)
            return
        loop.default_exception_handler(context)

    async def run(self):
        previous_handler = self.loop.get_exception_handler()
        self.loop.set_exception_handler(self.handle_asyncio_exception)
        self.mqtt.connect_async(self.opts.host, self.opts.port, keepalive=60)
        self.mqtt.loop_start()
        try:
            await asyncio.gather(self.modbus_polling(), self.polling(), self.pack_polling(), self.worker())
        finally:
            self.mqtt.loop_stop()
            self.mqtt.disconnect()
            self.loop.set_exception_handler(previous_handler)

async def main():
    logging.basicConfig(level=logging.DEBUG if os.getenv("DEBUG") == "true" else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Library currently logs BLE session keys at INFO: suppress sensitive material.
    logging.getLogger("bluetti_bt_lib.bluetooth.encryption").setLevel(logging.ERROR)
    # Home Assistant add-on image intentionally has no bluetoothctl; Bleak defaults to BlueZ 5.55+.
    # Only silence this known capability probe warning, preserving other BLE warnings.
    logging.getLogger("bleak.backends.bluezdbus.version").setLevel(logging.ERROR)
    opts = Settings.load()
    bridge = Bridge(opts)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, bridge.stop_event.set)
        except NotImplementedError:
            pass
    await bridge.run()

if __name__ == "__main__":
    asyncio.run(main())