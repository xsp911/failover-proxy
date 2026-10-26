"""Single-worker bounded FIFO admission, held until client response completes."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass


class CapacityUnavailable(Exception):
    pass


class AdmissionCancelled(Exception):
    pass


@dataclass
class Waiter:
    event: asyncio.Event


class Admission:
    def __init__(self, counters, wait_seconds=3.0, max_waiters=32):
        self.counters = counters
        self.wait_seconds = wait_seconds
        self.max_waiters = max_waiters
        self.waiters = deque()
        self.counters.setdefault("queued", 0)

    def _signal_head(self):
        if self.waiters:
            self.waiters[0].event.set()

    async def acquire(self, limit, disconnected=None):
        # All state mutations happen without awaits on this worker's event loop.
        if not self.waiters and self.counters["n"] < limit:
            self.counters["n"] += 1
            return self._lease()
        if len(self.waiters) >= self.max_waiters:
            raise CapacityUnavailable()
        waiter = Waiter(asyncio.Event())
        self.waiters.append(waiter)
        self.counters["queued"] = len(self.waiters)
        deadline = asyncio.get_running_loop().time() + self.wait_seconds
        try:
            while True:
                if self.waiters[0] is waiter and self.counters["n"] < limit:
                    self.waiters.popleft()
                    self.counters["queued"] = len(self.waiters)
                    self.counters["n"] += 1
                    self._signal_head()
                    return self._lease()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise CapacityUnavailable()
                waiter.event.clear()
                try:
                    await asyncio.wait_for(waiter.event.wait(), min(0.1, remaining))
                except TimeoutError:
                    pass
                if disconnected is not None and await disconnected():
                    raise AdmissionCancelled()
        finally:
            if waiter in self.waiters:
                self.waiters.remove(waiter)
                self.counters["queued"] = len(self.waiters)
                self._signal_head()

    def _lease(self):
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                self.counters["n"] -= 1
                self._signal_head()

        return release
