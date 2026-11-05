# Bluetti2MQTT Plus - AC500 Hybrid (v1.0.0-beta5)

**Experimental third Home Assistant add-on**, independent of:
- Bluetti2MQTT Plus 3.0.3 (legacy);
- Bluetti2MQTT Plus - AC500 Secure BLE 4.0.1 (stable).

Do not run the Hybrid add-on at the same time as either existing bridge for the
same AC500 serial number and MQTT topic namespace.

## Transport split

**Modbus TCP / unit ID 1, read-only**: AC output, AC input/grid charging power,
total PV, battery SOC, voltage, current, grid frequency and PV string power.
Registers follow the generated AC500 profile from
[bluetti-community/bluetti-modbus](https://github.com/bluetti-community/bluetti-modbus/blob/main/src/bluetti_modbus_lib/devices/ac500.py).

**Encrypted BLE (bluetti-bt-lib 0.1.8)**: B300 pack SOC/voltage/16 cell values,
remaining settings/diagnostics, and **all device commands** (including
grid_charge_amps, SOC thresholds, output and UPS controls).

Existing MQTT topics and retained Home Assistant Discovery IDs are preserved.
Modbus wins for its mapped fields when fresh; BLE is a fallback when TCP is
unavailable. When Modbus is down, generic BLE polling returns to 15 seconds.
When Modbus is up, BLE generic polling uses poll_sec (120 seconds by default).
B300 scanning retains 600-second interval and hourly full scan.

## Install / configure

From the same GitHub add-on repository select **AC500 Hybrid**.
Stop Secure BLE 4.0.1 first, then configure these settings:

- modbus_host: LAN IP of the AC500 (mandatory for hybrid telemetry)
- modbus_port: 502
- modbus_poll_sec: 5 (2+ seconds supported; start with 5 because another
  Home Assistant Modbus integration is already polling the device)
- bt_mac / device_serial: same as Secure BLE 4.0.1
- MQTT broker and credentials: same as Secure BLE 4.0.1
- allow_writes: true ONLY if you want MQTT commands
- poll_sec: 120 (non-Modbus BLE telemetry; does not delay commands)
- pack_poll_sec: 600, pack_scan_sec: 3600

**Commands are MQTT event driven, not tied to poll_sec**; an ongoing BLE
session or BLE timeout can still delay a command. No real-time latency
guarantee is possible while the BLE session is busy.

## Important beta limitations

- Needs hardware validation on the target AC500 before calling it stable.
- If the AC500 allows a limited number of simultaneous TCP connections,
  temporarily pause the existing Modbus integration during testing.
- The Modbus AC500 profile exposes max pack slots, not necessarily the number
  of installed B300 packs. Per-pack values continue through BLE.
- The Modbus source is read-only by design; do not infer its SOC registers
  are safe for writing.
- The first BLE cold poll may occur before the first Modbus success. MQTT
  consumers must rely on their own staleness checks in case both transports fail.

## v1.0.0-beta5 — MQTT command priority
- Abort B300's noncritical 10-second settle wait when an MQTT command arrives; restore B300 selector before releasing the BLE session.
- FC06 write echo followed by FC03 read-back **within the same encrypted connection**; publish the actual read-back state only if verified.
- Command queue dispatch latency logged; unsuccessful read-back triggers a prompt full BLE recheck.
- Modbus telemetry and normal BLE/B300 scheduling unchanged.

## v1.0.0-beta5 — delayed FC06 state verification
- FC06 echo means acknowledged, not necessarily applied. The AC500 may keep
  returning the previous register value for several seconds.
- Three bounded FC03 read-back attempts in the SAME encrypted BLE session;
  never repeat FC06 automatically.
- If a newer MQTT command is queued, stop verifying the older command so it
  can execute promptly.
- If the target is still not observed, log the target and observed values as
  **pending**, then request the existing BLE polling reconciliation.
- The command state is never set optimistically from the requested payload.

## v1.0.0-beta5 — high-frequency MQTT setpoints

The Home Assistant surplus automation **does not need to be throttled**. The
bridge now maintains a bounded **last-value-wins mailbox per physical BLE
register** (aliases coalesce by address):

- Each new setpoint replaces any pending older setpoint for that same register.
  Different registers remain distinct; their latest commands are dispatched in
  arrival order. An active BLE operation is never force-cancelled.
- Repeated events matching the in-flight target discard obsolete alternatives.
- Setpoints read back successfully from hardware are deduplicated for 30 s;
  a different value is never suppressed by this cache.
- Pending commands older than 45 s are discarded instead of being executed
  dangerously late after a 60-second BLE timeout. Repeated same-value events
  refresh the request timestamp. A fresh command can always be queued.
- The existing 120 s BLE telemetry timer is **not** a command timer; commands
  continue to wake the BLE command worker as events arrive.

If the device does not respond to encrypted BLE, no software can guarantee
instant writes. The mailbox prevents a backlog while communication recovers.

## v1.0.0-beta5 — faster charge-current state reconciliation

The AC500 commonly acknowledges FC06 before its register 3019 reports the
new charge-current value. Repeated FC03 verification within a single BLE
session holds the Bluetooth lock but often observes the old value.

- Attempt at most one immediate FC03 read-back after FC06; **never repeat
  FC06** just to verify it.
- If not yet verified, request the normal BLE poll promptly after releasing
  the encrypted session. The poll publishes the **actual** register 3019 value
  to Home Assistant and logs `BLE hardware grid_charge_amps=<n> A`.
- Keep beta4 last-value-wins mailbox, physical B300 scan preemption, and
  independent Modbus TCP polling unchanged.
- Do not publish requested charge current as confirmed hardware state before
  the register is actually observed.

Actual update latency is limited by the AC500 BLE handshake, firmware and
hardware settling time; no instantaneous acknowledgement is assumed.
