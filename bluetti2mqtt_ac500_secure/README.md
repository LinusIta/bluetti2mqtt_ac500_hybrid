# AC500 Hybrid Secure — technical preview 0.1.0-beta1

Independent add-on. The existing `bluetti2mqtt_ac500_hybrid` is untouched and does not require a Bluetti license.

## Requirements

Linux x86-64 Bluetti encrypted Bluetooth module V1.0.0-L and your **own** authorization, obtained from Bluetti. Place these **original** files in Home Assistant `/config/bluetti_secure/`:

- `_bluetti_crypt.so`
- `bluetti_crypt.py`
- `bluetti_device_licence.csv` (rename your personal authorization CSV locally)

No binary or license is committed. On startup the original Linux library is checked against the exact tested SHA256 and copied to `/run/bluetti_secure_module`, where AC500 register map entries 3019 and 170–172 are added. The originals remain untouched. The local patch replaces mappings 6184–6185 and 6181–6182, and MUST NOT be applied to other models or library versions.

## Test procedure

The add-on is experimental. Stop the legacy add-on before starting Secure: both use the same BLE interface and MQTT topics. Configure MAC, serial, Modbus host and MQTT settings, with `allow_writes: false` initially. **Do not use this as a production replacement yet.**

The source intentionally retains MQTT state prefix `bluetti/state/AC500-<serial>/` and command prefix `bluetti/command/AC500-<serial>/`.

## Tested separately on AC500

Encrypted BLE authentication; repeated FC03 queries; register 3019 FC16 change with subsequent FC03 confirmation; FC03 170 x3. The full packaged add-on has not yet been hardware-tested.

## Known omissions

B300 pack scanning, comprehensive legacy sensor discovery and full state parity are not implemented in beta1. Other control writes are encryption-accepted but not hardware-validated. A missing FC16 acknowledgement is not proof of failure; read-back is required, and uncertain writes are never retried automatically.

**Never include personal authorization files or proprietary vendor binaries in public commits or issues.**
