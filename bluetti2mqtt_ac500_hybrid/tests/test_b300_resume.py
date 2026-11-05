"""Regression tests for B300 progress and bridge preemption/restart integration."""
import ast
import asyncio
import logging
import sys
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "rootfs" / "app"))
from pack_scan_progress import PackScanProgress


class TestPackProgress(unittest.TestCase):
    def test_preempted_scan_resumes_from_unfinished_slot(self):
        p = PackScanProgress.begin("full", range(1, 7))
        p.record(1, populated=False)
        p.record(2, populated=True)
        self.assertEqual(p.remaining, [3, 4, 5, 6])
        self.assertFalse(p.complete)
        self.assertEqual(p.acquired, {2})
        self.assertEqual(p.empty, {1})
        for slot in (3, 4, 5, 6):
            p.record(slot, populated=True)
        self.assertTrue(p.complete)

    def test_failure_is_kept_pending_and_later_retry_succeeds(self):
        p = PackScanProgress.begin("active", [2, 3])
        p.record(2, populated=True)
        p.failed(3)
        self.assertFalse(p.complete)
        self.assertEqual(p.remaining, [3])
        self.assertEqual(p.failures, {3})
        p.record(3, populated=True)
        self.assertTrue(p.complete)
        self.assertEqual(p.failures, set())

    def test_out_of_order_or_invalid_scan_is_rejected(self):
        with self.assertRaises(ValueError):
            PackScanProgress.begin("bad", [2])
        with self.assertRaises(ValueError):
            PackScanProgress.begin("full", [0])
        p = PackScanProgress.begin("full", [1, 2])
        with self.assertRaises(ValueError):
            p.record(2, populated=True)


class FakeReadRegister:
    def __init__(self, address, count):
        self.address = address


class FakeReader:
    def __init__(self, slot, behaviour, pending):
        self.slot = slot
        self.behaviour = behaviour
        self.pending = pending
        self.packs = {}
        self.empty_slots = set()
        self.restore_ok = True
        self.was_preempted = False

    async def read(self, **kwargs):
        if self.behaviour == "preempt":
            self.was_preempted = True
            self.pending.set()
            return None
        if self.behaviour == "error":
            return None
        if self.behaviour == "unrestored":
            self.restore_ok = False
            return None
        if self.behaviour == "empty":
            self.empty_slots.add(self.slot)
        elif self.behaviour == "populated":
            self.packs[self.slot] = {
                "percent": 50 + self.slot,
                "voltage": 52.0,
                "voltages": [3.25] * 16,
            }
        else:
            raise AssertionError("Unexpected behaviour")
        return {3006: b"test"}


class FakeMQTT:
    def __init__(self):
        self.messages = []

    def publish(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class ScanBridge:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.visited = []
        self.pending_write = asyncio.Event()
        self.pack_scan_request = asyncio.Event()
        self.ble_session_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.pack_scan = None
        self.pack_resume_delay = None
        self.active_pack_slots = set()
        self.last_full_pack_scan = 0
        self.opts = types.SimpleNamespace(pack_poll_sec=600, pack_poll_enabled=False, pack_scan_sec=3600)
        self.prefix = "bluetti/state/test/"
        self.mqtt = FakeMQTT()
        self.publications = []
        self.discoveries = []

    def new_reader(self, cls, *, targets, priority_event):
        slot = targets[0]
        self.visited.append(slot)
        kind = self.behaviour.pop(slot, "populated")
        return FakeReader(slot, kind, priority_event)

    async def wait_for_pending_writes(self):
        # The production worker is responsible for clearing pending_write.
        # A simulated preempt must be cleared explicitly between invocations.
        return

    def publish_pack_discovery(self, slot):
        self.discoveries.append(slot)

    def publish(self, field, value):
        self.publications.append((field, value))


def load_bridge_scan_methods():
    root = Path(__file__).parents[1] / "rootfs" / "app" / "bridge.py"
    parsed = ast.parse(root.read_text())
    bridge = next(n for n in parsed.body
                  if isinstance(n, ast.ClassDef) and n.name == "Bridge")
    names = {"publish_pack_sample", "poll_packs_once", "pack_polling"}
    methods = [n for n in bridge.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    # Re-use the production methods without importing Home Assistant BLE libs.
    compiled = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(
        name="ScanBridge", bases=[ast.Name(id="ScanBridge", ctx=ast.Load())],
        keywords=[], body=methods, decorator_list=[])], type_ignores=[]))

    async def quick_sleep(*args):
        await asyncio.sleep(0)

    env = {
        "ScanBridge": ScanBridge,
        "PackScanProgress": PackScanProgress,
        "Any": __import__("typing").Any,
        "ReadableRegisters": FakeReadRegister,
        "PACK_SELECTOR": 3006,
        "PackPollingReader": object,
        "time": time,
        "asyncio": types.SimpleNamespace(sleep=quick_sleep,
                                         wait_for=asyncio.wait_for,
                                         TimeoutError=asyncio.TimeoutError),
        "LOG": logging.getLogger("fake-b300"),
        "json": __import__("json"),
    }
    exec(compile(compiled, str(root), "exec"), env)
    return env["ScanBridge"]


BridgeUnderTest = load_bridge_scan_methods()


class TestResumableB300(unittest.IsolatedAsyncioTestCase):
    async def test_preemption_keeps_samples_and_resumes_only_pending_slots(self):
        bridge = BridgeUnderTest({1: "empty", 2: "populated", 3: "preempt"})
        await bridge.poll_packs_once(True)
        self.assertEqual(bridge.visited, [1, 2, 3])
        self.assertEqual(bridge.pack_scan.remaining, [3, 4, 5, 6])
        self.assertEqual(bridge.active_pack_slots, {2})
        self.assertEqual(bridge.last_full_pack_scan, 0)
        self.assertEqual(bridge.pack_resume_delay, 15)
        self.assertEqual(len(bridge.mqtt.messages), 1)
        self.assertFalse(any(field == "battery_pack_1_percent"
                             for field, value in bridge.publications))

        bridge.pending_write.clear()  # worker has completed its MQTT command
        await bridge.poll_packs_once(True)
        self.assertEqual(bridge.visited, [1, 2, 3, 3, 4, 5, 6])
        self.assertIsNone(bridge.pack_scan)
        self.assertGreater(bridge.last_full_pack_scan, 0)
        self.assertEqual(set(bridge.discoveries), {2, 3, 4, 5, 6})
        self.assertEqual(len(bridge.mqtt.messages), 5)

    async def test_read_failure_never_marks_full_scan_complete(self):
        bridge = BridgeUnderTest({1: "populated", 2: "error"})
        await bridge.poll_packs_once(True)
        self.assertEqual(bridge.visited, [1, 2])
        self.assertEqual(bridge.pack_scan.remaining, [2, 3, 4, 5, 6])
        self.assertEqual(bridge.pack_scan.failures, {2})
        self.assertEqual(bridge.pack_resume_delay, 600)
        self.assertEqual(bridge.last_full_pack_scan, 0)
        await bridge.poll_packs_once(True)
        self.assertEqual(bridge.visited, [1, 2, 2, 3, 4, 5, 6])
        self.assertIsNone(bridge.pack_scan)
        self.assertGreater(bridge.last_full_pack_scan, 0)

    async def test_disabled_pack_scan_runs_only_on_manual_request(self):
        bridge = BridgeUnderTest({})
        task = asyncio.create_task(bridge.pack_polling())
        try:
            await asyncio.sleep(0.02)
            self.assertEqual(bridge.visited, [])
            bridge.pack_scan_request.set()
            await asyncio.sleep(0.08)
            self.assertEqual(bridge.visited, [1, 2, 3, 4, 5, 6])
            self.assertIsNone(bridge.pack_scan)
            self.assertFalse(bridge.pack_scan_request.is_set())
            await asyncio.sleep(0.02)
            self.assertEqual(len(bridge.visited), 6)
        finally:
            bridge.stop_event.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_pending_mqtt_command_prevents_any_ble_scan(self):
        bridge = BridgeUnderTest({})
        bridge.pending_write.set()
        await bridge.poll_packs_once(True)
        self.assertEqual(bridge.visited, [1])
        self.assertEqual(bridge.pack_scan.remaining, [1, 2, 3, 4, 5, 6])
        self.assertEqual(bridge.last_full_pack_scan, 0)
        self.assertEqual(bridge.pack_resume_delay, 15)


if __name__ == "__main__":
    unittest.main()
