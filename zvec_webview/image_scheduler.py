"""Bounded, priority-aware gate for the image render slots.

Decoding a source file into a thumbnail or preview is the expensive part of
serving an image, so the registry runs only a fixed number of renders at once.
A plain semaphore makes a late request wait behind whatever queued first; this
gate instead hands a free slot to the highest-priority waiter, keeps arrival
order inside one priority, and periodically gives the oldest waiter a fairness
turn so low priorities cannot starve.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Final

_CLOSED_MESSAGE: Final = "图片预览服务已关闭。"
_FULL_MESSAGE: Final = "图片预览队列已满，请稍后重试。"

_PRIORITY_RANK: Final[dict[str, int]] = {
    "background": 0,
    "prefetch": 1,
    "visible": 2,
    "current": 3,
}
_DEFAULT_FAIRNESS_LIMIT: Final = 8


class PriorityGateError(RuntimeError):
    """Base class for scheduling-gate failures."""


class PriorityGateFull(PriorityGateError):
    """The bounded wait queue cannot accept another render request."""


class PriorityGateClosed(PriorityGateError):
    """The gate closed while a request was queued or waiting."""


@dataclass(slots=True, eq=False)
class RenderTicket:
    """One render request whose priority may change while it waits.

    Identity equality keeps ``list.remove`` from mistaking two tickets with the
    same fields for each other.
    """

    priority: str
    granted: bool = False
    waiting: bool = False


class PriorityGate:
    """Grant a bounded number of slots in priority order.

    ``enqueue`` never blocks and only queues a ticket; ``wait`` blocks until the
    ticket owns a slot or the gate closes.  A ticket can be created before it is
    queued and promoted in the meantime, which is what lets an in-flight disk
    check hand its slot to a higher-priority caller.
    """

    def __init__(
        self,
        capacity: int,
        max_pending: int,
        *,
        fairness_limit: int = _DEFAULT_FAIRNESS_LIMIT,
    ) -> None:
        if isinstance(capacity, bool) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        if isinstance(max_pending, bool) or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        if isinstance(fairness_limit, bool) or fairness_limit < 1:
            raise ValueError("fairness_limit must be a positive integer")
        self._condition = threading.Condition()
        self._capacity = capacity
        self._max_pending = max_pending
        self._fairness_limit = fairness_limit
        self._active = 0
        self._waiting: list[RenderTicket] = []
        self._prioritized_streak = 0
        self._closed = False

    @property
    def pending(self) -> int:
        """Number of render requests currently queued, for diagnostics."""

        with self._condition:
            return len(self._waiting)

    def new_ticket(self, priority: str) -> RenderTicket:
        if priority not in _PRIORITY_RANK:
            raise ValueError(f"unsupported priority: {priority!r}")
        return RenderTicket(priority)

    def enqueue(self, ticket: RenderTicket) -> None:
        """Queue a ticket and grant any free slots without blocking."""

        with self._condition:
            if self._closed:
                raise PriorityGateClosed(_CLOSED_MESSAGE)
            if ticket.granted or ticket.waiting:
                return
            if len(self._waiting) >= self._max_pending:
                raise PriorityGateFull(_FULL_MESSAGE)
            ticket.waiting = True
            self._waiting.append(ticket)
            self._dispatch_locked()
            self._condition.notify_all()

    def wait(self, ticket: RenderTicket) -> None:
        """Block until the ticket owns a slot or the gate closes."""

        with self._condition:
            while not ticket.granted:
                if self._closed:
                    raise PriorityGateClosed(_CLOSED_MESSAGE)
                self._condition.wait()

    def upgrade(self, ticket: RenderTicket, priority: str) -> None:
        """Raise a ticket's priority, even if it is not queued yet."""

        if priority not in _PRIORITY_RANK:
            return
        with self._condition:
            if _PRIORITY_RANK[priority] <= _PRIORITY_RANK[ticket.priority]:
                return
            ticket.priority = priority
            self._dispatch_locked()
            self._condition.notify_all()

    def release(self) -> None:
        """Return one slot; a running render is never preempted."""

        with self._condition:
            if self._active > 0:
                self._active -= 1
            self._dispatch_locked()
            self._condition.notify_all()

    def close(self) -> None:
        """Wake every waiter; already-granted renders still finish."""

        with self._condition:
            self._closed = True
            for ticket in self._waiting:
                ticket.waiting = False
            self._waiting.clear()
            self._condition.notify_all()

    def _dispatch_locked(self) -> None:
        if self._closed:
            return
        while self._waiting and self._active < self._capacity:
            chosen = self._choose_locked()
            self._waiting.remove(chosen)
            chosen.waiting = False
            chosen.granted = True
            self._active += 1

    def _choose_locked(self) -> RenderTicket:
        oldest = self._waiting[0]
        best = oldest
        for ticket in self._waiting:
            if _PRIORITY_RANK[ticket.priority] > _PRIORITY_RANK[best.priority]:
                best = ticket
        if best is oldest:
            self._prioritized_streak = 0
            return best
        if self._prioritized_streak >= self._fairness_limit:
            self._prioritized_streak = 0
            return oldest
        self._prioritized_streak += 1
        return best


__all__ = [
    "PriorityGate",
    "PriorityGateClosed",
    "PriorityGateError",
    "PriorityGateFull",
    "RenderTicket",
]
