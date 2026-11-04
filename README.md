# Bluetti2MQTT Plus – AC500 Hybrid

Independent Home Assistant add-on repository for **Bluetti AC500 Hybrid (Modbus TCP + encrypted BLE)**.

**Current version:** `1.0.0-beta2` (experimental; device validation pending).

The add-on lives in [`bluetti2mqtt_ac500_hybrid/`](bluetti2mqtt_ac500_hybrid/README.md) and can be installed from the Home Assistant add-on store using this repository URL:

`https://github.com/LinusIta/bluetti2mqtt_ac500_hybrid`

## Transport
- **Modbus TCP (read-only)** for primary telemetry.
- **Encrypted BLE** for B300 pack details and event-driven commands.
- Existing MQTT topics and Home Assistant discovery identifiers are preserved; **do not run** this add-on simultaneously with Secure BLE on the same AC500.

See [installation, settings, and limitations](bluetti2mqtt_ac500_hybrid/README.md).

## Other projects
The independently maintained legacy Bluetti2MQTT Plus 3.0.3 and AC500 Secure BLE 4.0.1 add-ons remain at [LinusIta/bluetti2mqtt_plus](https://github.com/LinusIta/bluetti2mqtt_plus).

## Test status
Protocol tests against a fake Modbus TCP server and Python syntax checks are automated in GitHub Actions. Real hardware behavior requires verification on an AC500.
