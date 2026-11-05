"""Resumable B300 scan bookkeeping, independent of BLE and MQTT.

Completed slots are never re-read within the same scan. A preempted or
failed slot remains pending; no partial full scan is reported as complete.
Runtime state is in memory only and resets safely on add-on restart.
"""
from dataclasses import dataclass, field


@dataclass
class PackScanProgress:
    kind: str
    remaining: list[int]
    acquired: set[int] = field(default_factory=set)
    empty: set[int] = field(default_factory=set)
    failures: set[int] = field(default_factory=set)

    @classmethod
    def begin(cls, kind, slots):
        if kind not in ("full", "active"):
            raise ValueError("Invalid B300 scan kind")
        items = list(dict.fromkeys(int(slot) for slot in slots))
        if not items or any(slot not in range(1, 7) for slot in items):
            raise ValueError("Invalid B300 scan slots")
        return cls(kind=kind, remaining=items)

    @property
    def complete(self):
        return not self.remaining

    def record(self, slot, *, populated):
        if not self.remaining or self.remaining[0] != slot:
            raise ValueError("B300 scan slot completed out of order")
        self.remaining.pop(0)
        self.failures.discard(slot)
        if populated:
            self.acquired.add(slot)
            self.empty.discard(slot)
        else:
            self.empty.add(slot)
            self.acquired.discard(slot)

    def failed(self, slot):
        if slot not in self.remaining:
            raise ValueError("Failed B300 slot is not pending")
        self.failures.add(slot)
