"""Regression tests: fast MQTT setpoints must never backlog old BLE writes."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "rootfs", "app")))
from command_mailbox import CommandMailbox


class TestMailbox(unittest.TestCase):
    def test_last_setpoint_wins_during_ble_timeout(self):
        box = CommandMailbox(max_age=45)
        self.assertEqual(box.put(3019, "grid_charge_amps", "7", 7, 0), "queued")
        self.assertEqual(box.pop(0)[3], 7)
        # While BLE handshakes or timeouts: all older pending setpoints go.
        for index, target in enumerate((8, 7, 6, 5, 4, 3), start=1):
            box.put(3019, "grid_charge_amps", str(target), target, index * 10)
        box.finish(3019, 7)
        command = box.pop(61)
        self.assertEqual(command[3], 3)
        self.assertFalse(box.has_pending)

    def test_same_inflight_cancels_older_pending_target(self):
        box = CommandMailbox()
        box.put(3019, "grid_charge_amps", "7", 7, 0)
        box.pop(0)
        box.put(3019, "grid_charge_amps", "5", 5, 1)
        self.assertEqual(box.put(3019, "grid_charge_amps", "7", 7, 2), "already_active")
        self.assertFalse(box.has_pending)

    def test_aliases_share_physical_register(self):
        box = CommandMailbox()
        box.put(3007, "ctrl_ac", "ON", 1, 0)
        self.assertEqual(box.put(3007, "ac_output_on", "OFF", 0, 1), "replaced")
        self.assertEqual(box.pop(1)[3], 0)

    def test_unique_addresses_keep_latest_arrival_order(self):
        box = CommandMailbox()
        box.put(3015, "battery_range_start", "100", 100, 0)
        box.put(3016, "battery_range_end", "100", 100, 1)
        box.put(3015, "battery_range_start", "90", 90, 2)
        self.assertEqual(box.pop(2)[0], 3016)
        box.finish(3016, 100)
        self.assertEqual(box.pop(3)[3], 90)

    def test_charge_current_goes_first_even_if_soc_arrived_first(self):
        box = CommandMailbox()
        box.put(3016, "battery_range_end", "100", 100, 0)
        box.put(3015, "battery_range_start", "20", 20, 0.01)
        box.put(3019, "grid_charge_amps", "3", 3, 0.02)
        self.assertEqual(box.pop(1)[0], 3019)
        box.finish(3019, 3)
        self.assertEqual(box.pop(2)[0], 3016)
        box.finish(3016, 100)
        self.assertEqual(box.pop(3)[0], 3015)

    def test_latest_amps_preempt_older_pending_without_dropping_soc(self):
        box = CommandMailbox()
        box.put(3015, "battery_range_start", "20", 20, 0)
        box.put(3019, "grid_charge_amps", "6", 6, 0.1)
        box.put(3019, "grid_charge_amps", "3", 3, 0.2)
        self.assertEqual(box.pop(1)[3], 3)
        box.finish(3019, 3)
        self.assertEqual(box.pop(2)[0], 3015)

    def test_expired_command_not_executed(self):
        box = CommandMailbox(max_age=45)
        box.put(3019, "grid_charge_amps", "8", 8, 0)
        self.assertIsNone(box.pop(61))
        self.assertFalse(box.has_pending)

    def test_identical_setpoint_refreshes_pending_age(self):
        box = CommandMailbox(max_age=45)
        box.put(3019, "grid_charge_amps", "8", 8, 0)
        self.assertEqual(box.put(3019, "grid_charge_amps", "8", 8, 45), "duplicate")
        self.assertEqual(box.pop(70)[3], 8)

    def test_verified_duplicate_suppressed_for_short_window(self):
        box = CommandMailbox(verified_ttl=30)
        box.mark_verified(3019, 8, 0)
        self.assertEqual(box.put(3019, "grid_charge_amps", "8", 8, 15), "recently_verified")
        self.assertFalse(box.has_pending)
        self.assertEqual(box.put(3019, "grid_charge_amps", "8", 8, 31), "queued")

    def test_different_value_after_verified_is_queued(self):
        box = CommandMailbox()
        box.mark_verified(3019, 8, 0)
        self.assertEqual(box.put(3019, "grid_charge_amps", "6", 6, 10), "queued")
        self.assertEqual(box.pop(10)[3], 6)


if __name__ == "__main__":
    unittest.main()
