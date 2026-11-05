"""Bounded latest-desired-value mailbox for encrypted BLE device commands.

No asyncio dependencies: single Home Assistant event-loop thread owns this
mailbox. Different aliases for the same hardware register share one slot.
"""
from collections import OrderedDict
import logging

LOG = logging.getLogger("bluetti2mqtt_plus")


class CommandMailbox:
    def __init__(self, max_age=45.0, verified_ttl=30.0):
        self.max_age = float(max_age)
        self.verified_ttl = float(verified_ttl)
        self.pending = OrderedDict()
        self.active = None
        self.verified = {}

    @property
    def has_pending(self):
        return bool(self.pending)

    def put(self, address, field, payload, numeric, now):
        address = int(address)
        numeric = int(numeric)
        old = self.pending.get(address)
        # A new event always supersedes any older queued event for this same
        # hardware register, irrespective of MQTT alias/entity name.
        if self.active == (address, numeric):
            self.pending.pop(address, None)
            return "already_active"
        # A fresh, *read-back-verified* value may absorb identical events
        # after the active BLE command is no longer in progress.
        cached = self.verified.get(address)
        if (self.active is None and cached is not None and
                cached[0] == numeric and 0 <= now - cached[1] < self.verified_ttl):
            self.pending.pop(address, None)
            return "recently_verified"
        if old is not None and old[2] == numeric:
            # Repeated MQTT events refresh desired-value freshness, avoiding
            # expiration during an ongoing 60s BLE timeout.
            self.pending[address] = (field, payload, numeric, now)
            self.pending.move_to_end(address)
            return "duplicate"
        self.pending[address] = (field, payload, numeric, now)
        self.pending.move_to_end(address)
        return "replaced" if old is not None else "queued"

    def pop(self, now):
        while self.pending:
            # Charging current is the time-critical surplus control. When
            # multiple MQTT commands arrive together, run 3019 before SOC
            # configuration; an already active FC06 is never interrupted.
            if 3019 in self.pending:
                address = 3019
                field, payload, numeric, received_at = self.pending.pop(address)
            else:
                address, (field, payload, numeric, received_at) = self.pending.popitem(last=False)
            if now - received_at > self.max_age:
                # Never execute an obsolete command after a long BLE timeout.
                LOG.warning("Expired MQTT setpoint %s=%s (%.1fs queued); not sent to BLE",
                            field, payload, now - received_at)
                continue
            self.active = (address, numeric)
            return address, field, payload, numeric, received_at
        return None

    def mark_verified(self, address, numeric, now):
        self.verified[int(address)] = (int(numeric), now)

    def finish(self, address, numeric):
        if self.active == (address, numeric):
            self.active = None
