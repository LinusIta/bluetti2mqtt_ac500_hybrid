"""AC500 Hybrid Secure preview. Do not run together with the legacy bridge."""
import asyncio
import json
import logging
import os
import re
import signal
import struct
import sys
from pathlib import Path

import paho.mqtt.client as mqtt
from patch_module import prepare

LOG = logging.getLogger("ac500_secure")

FIELDS = {
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
BOOLEANS = {"ac_output_on","ctrl_ac","dc_output_on","ctrl_dc",
            "grid_charge_on","time_control_on"}
UPS = {"CUSTOMIZED":1, "PV_PRIORITY":2, "STANDARD":3, "TIME_CONTROL":4}
DISPLAY = {"THIRTY_SECONDS":2, "ONE_MINUTE":3, "FIVE_MINUTES":4, "NEVER":5,
           "30 S":2, "1 MIN":3, "5 MIN":4, "30 SECONDS":2, "1 MINUTE":3, "5 MINUTES":4}
DISPLAY_NAMES = {2:"THIRTY_SECONDS",3:"ONE_MINUTE",4:"FIVE_MINUTES",5:"NEVER"}
BLE_READS = [(43,1),(41,1),(72,1),(73,1),(75,1),(76,1),(78,1),(79,1),
             (86,1),(87,1),(88,1),(91,2),(170,3),
             (3001,1),(3007,1),(3008,1),(3011,1),(3013,1),
             (3015,2),(3019,1),(3061,1)]
LEGACY = {
    "power_generation":(41,1), "internal_current_one":(72,1),
    "internal_power_one":(73,0), "internal_current_two":(75,1),
    "internal_power_two":(76,0), "internal_current_three":(78,1),
    "internal_power_three":(79,0), "internal_dc_input_voltage":(86,1),
    "internal_dc_input_power":(87,0), "internal_dc_input_current":(88,1),
    "pack_num_max":(91,0), "total_battery_voltage":(92,1)
}

class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.serial = str(cfg["device_serial"])
        self.prefix = f"bluetti/state/AC500-{self.serial}/"
        self.command_prefix = f"bluetti/command/AC500-{self.serial}/"
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=100)
        self.stop = asyncio.Event()
        self.values = {}
        self.ble = None
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                client_id="bluetti-secure-" + self.serial)
        if cfg.get("mqtt_username"):
            self.mqtt.username_pw_set(cfg["mqtt_username"], cfg.get("mqtt_password",""))
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_message = self.on_message

    def state(self, field, value):
        self.mqtt.publish(self.prefix + field, str(value), qos=1, retain=True)

    def on_connect(self, client, _userdata, _flags, code, _properties):
        if code != 0:
            LOG.error("MQTT connection error: %s", code)
            return
        client.subscribe(self.command_prefix + "+", qos=1)
        self.loop.call_soon_threadsafe(self.discovery)

    def on_message(self, _client, _userdata, message):
        if message.retain or not message.topic.startswith(self.command_prefix):
            return
        field = message.topic[len(self.command_prefix):]
        if field not in FIELDS:
            return
        try:
            payload = message.payload.decode("utf-8").strip()
        except UnicodeDecodeError:
            return
        if self.cfg.get("allow_writes") is not True:
            LOG.warning("Write blocked by allow_writes=false")
            return
        self.loop.call_soon_threadsafe(self.enqueue, field, payload)

    def enqueue(self, field, payload):
        try:
            self.queue.put_nowait((field, payload))
        except asyncio.QueueFull:
            LOG.error("MQTT command queue full; discarded %s", field)

    def discovery(self):
        if self.cfg.get("ha_config") == "none":
            return
        device = {"identifiers":[self.serial], "manufacturer":"Bluetti",
                  "model":"AC500", "name":"AC500 " + self.serial}
        for field, (addr, lo, hi) in FIELDS.items():
            if field.startswith("ctrl_"):
                continue
            domain = ("switch" if field in BOOLEANS else
                      "select" if field in ("ups_mode","auto_sleep_mode") else "number")
            item = {"name":field.replace("_"," ").title(),
                    "unique_id":f"{self.serial}_{field}",
                    "object_id":"AC500_"+field, "device":device,
                    "state_topic":self.prefix+field,
                    "command_topic":self.command_prefix+field}
            if domain == "number":
                item.update(min=lo,max=hi,step=1,mode="box")
            if field == "grid_charge_amps":
                item["unit_of_measurement"] = "A"
            if field == "ups_mode":
                item["options"] = list(UPS)
            if field == "auto_sleep_mode":
                item["options"] = list(DISPLAY_NAMES.values())
            self.mqtt.publish(
                f"homeassistant/{domain}/{self.serial}_{field}/config",
                json.dumps(item), qos=1, retain=True)

    def parse(self, field, text):
        address, low, high = FIELDS[field]
        value = text.strip().upper()
        if field in BOOLEANS:
            parsed = {"ON":1, "OFF":0, "TRUE":1, "FALSE":0, "1":1, "0":0}[value]
        elif field in ("ups_mode", "ctrl_ups_mode"):
            parsed = UPS[value] if value in UPS else int(value)
        elif field in ("auto_sleep_mode","ctrl_display_timeout"):
            parsed = DISPLAY[value] if value in DISPLAY else int(value)
        else:
            parsed = int(value)
        if not low <= parsed <= high:
            raise ValueError(f"{field}: outside {low}..{high}")
        if address == 3015 and 3016 in self.values and parsed > self.values[3016]:
            raise ValueError("SOC start exceeds SOC end")
        if address == 3016 and 3015 in self.values and parsed < self.values[3015]:
            raise ValueError("SOC end below SOC start")
        return address, parsed

    def control_state(self, address, value):
        self.values[address] = value
        for field, (addr,_,_) in FIELDS.items():
            if addr != address:
                continue
            if field in BOOLEANS:
                result = "ON" if value else "OFF"
            elif field in ("ups_mode","ctrl_ups_mode"):
                result = next((name for name, val in UPS.items() if val == value), str(value))
            elif field == "auto_sleep_mode":
                result = DISPLAY_NAMES.get(value,str(value))
            elif field == "ctrl_display_timeout":
                result = {2:"30 s",3:"1 min",4:"5 min",5:"Never"}.get(value,str(value))
            else:
                result = value
            self.state(field, result)

    async def writer(self):
        while True:
            field, payload = await self.queue.get()
            try:
                address, value = self.parse(field,payload)
                LOG.info("FC16 %s -> %s", field, value)
                confirmed = await self.ble.write_and_verify(
                    address, value, FIELDS[field][1], FIELDS[field][2])
                self.control_state(address, confirmed)
            except Exception:
                LOG.exception("Secure command %s rejected or unconfirmed", field)
            finally:
                self.queue.task_done()

    async def ble_poll(self):
        interval = int(self.cfg.get("poll_sec",120))
        while True:
            try:
                raw = {}
                for start, count in BLE_READS:
                    if not self.queue.empty():
                        break
                    try:
                        values = await self.ble.read(start, count)
                        raw.update({start+i: n for i,n in enumerate(values)})
                    except Exception as exc:
                        LOG.warning("BLE FC03 %d x%d failed: %s",start,count,exc)
                for name,(addr,places) in LEGACY.items():
                    if addr in raw and not (name=="total_battery_voltage" and self.cfg.get("modbus_host")):
                        self.state(name, raw[addr] / (10**places) if places else raw[addr])
                if 43 in raw and not self.cfg.get("modbus_host"):
                    self.state("total_battery_percent",raw[43])
                    self.state("battery_soc",raw[43])
                if 170 in raw and 172 in raw:
                    self.state("dc_input_voltage2",raw[170]/10)
                    self.state("dc_input_power2",raw[172])
                for address in sorted({a for a,_,_ in FIELDS.values()}):
                    if address in raw:
                        self.control_state(address,raw[address])
                LOG.info("BLE poll completed (%d registers)",len(raw))
            except Exception:
                LOG.exception("BLE poll error")
            await asyncio.sleep(interval)

    async def tcp_read(self, address, count):
        reader,writer = await asyncio.wait_for(
            asyncio.open_connection(self.cfg["modbus_host"],
                                    int(self.cfg.get("modbus_port",502))), 4)
        try:
            request = struct.pack(">HHHBBHH",1,0,6,1,3,address,count)
            writer.write(request)
            await asyncio.wait_for(writer.drain(),4)
            header = await asyncio.wait_for(reader.readexactly(7),4)
            tx,proto,length,unit = struct.unpack(">HHHB",header)
            if (tx,proto,unit) != (1,0,1) or not 3 <= length <= 253:
                raise ValueError("Invalid TCP MBAP response")
            pdu = await asyncio.wait_for(reader.readexactly(length-1),4)
            if pdu[0] != 3 or pdu[1] != count*2 or len(pdu) != 2+count*2:
                raise ValueError("Invalid TCP Modbus FC03 response")
            return struct.unpack(">"+"H"*count,pdu[2:])
        finally:
            writer.close()
            await writer.wait_closed()

    async def tcp_poll(self):
        interval = max(2,int(self.cfg.get("modbus_poll_sec",5)))
        while True:
            try:
                freq,grid = await self.tcp_read(50214,2)
                ac = (await self.tcp_read(50217,1))[0]
                pv = (await self.tcp_read(50004,1))[0]
                battery = await self.tcp_read(51002,3)
                pv1 = await self.tcp_read(50269,2)
                pv2 = (await self.tcp_read(50273,1))[0]
                ac_switch = (await self.tcp_read(57001,1))[0]
                dc_switch = (await self.tcp_read(57005,1))[0]
                soc = await self.tcp_read(57016,2)
                if not (0 <= battery[2] <= 100 and 250 <= battery[0] <= 700
                        and 0 <= soc[0] <= soc[1] <= 100):
                    raise ValueError("Implausible Modbus readings")
                states = {"ac_output_power":ac, "ac_input_power":grid,
                          "dc_input_power":pv, "total_battery_percent":battery[2],
                          "battery_soc":battery[2], "total_battery_voltage":battery[0]/10,
                          "total_battery_current":battery[1]/10,
                          "dc_input_power1":pv1[0], "dc_input_voltage1":pv1[1]/10,
                          "dc_input_power2":pv2}
                if freq:
                    states["ac_input_frequency"] = freq/100
                for field,value in states.items():
                    self.state(field,value)
                for address,value in ((3007,ac_switch),(3008,dc_switch),
                                      (3015,soc[0]),(3016,soc[1])):
                    self.control_state(address,value)
            except Exception as exc:
                LOG.warning("Modbus TCP poll failed: %s",exc)
            await asyncio.sleep(interval)

    async def run(self):
        module_dir = Path("/run/bluetti_secure_module")
        digest = prepare(Path("/config/bluetti_secure"),module_dir)
        LOG.info("Prepared authorized module (SHA256 %s)",digest)
        os.chdir(module_dir)
        sys.path.insert(0,str(module_dir))
        from secure_ble import SecureBLE
        self.ble = SecureBLE(self.cfg["bt_mac"])
        self.mqtt.connect(self.cfg.get("mqtt_host") or "core-mosquitto",
                          int(self.cfg.get("mqtt_port") or 1883),60)
        self.mqtt.loop_start()
        jobs = [asyncio.create_task(self.writer()),asyncio.create_task(self.ble_poll())]
        if self.cfg.get("modbus_host"):
            jobs.append(asyncio.create_task(self.tcp_poll()))
        try:
            await self.stop.wait()
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs,return_exceptions=True)
            await self.ble.close()
            self.mqtt.loop_stop()
            self.mqtt.disconnect()

async def main():
    config = json.loads(Path("/data/options.json").read_text())
    logging.basicConfig(level=logging.DEBUG if config.get("debug") else logging.INFO)
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", config.get("bt_mac","")):
        raise ValueError("Invalid bt_mac")
    if not re.fullmatch(r"\d{5,20}",str(config.get("device_serial",""))):
        raise ValueError("Invalid device_serial")
    bridge = Bridge(config)
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,bridge.stop.set)
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT,bridge.stop.set)
    await bridge.run()

if __name__ == "__main__":
    asyncio.run(main())
