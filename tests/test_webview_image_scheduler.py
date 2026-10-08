from __future__ import annotations

import unittest

from zvec_webview.image_scheduler import (
    PriorityGate,
    PriorityGateClosed,
    PriorityGateFull,
    RenderTicket,
)


def _gate(
    capacity: int = 1,
    max_pending: int = 8,
    *,
    fairness_limit: int = 8,
) -> PriorityGate:
    return PriorityGate(capacity, max_pending, fairness_limit=fairness_limit)


class PriorityGateTests(unittest.TestCase):
    def test_free_slots_are_granted_immediately_and_in_arrival_order(self) -> None:
        gate = _gate(capacity=2)
        first = gate.new_ticket("visible")
        second = gate.new_ticket("visible")
        third = gate.new_ticket("visible")

        gate.enqueue(first)
        gate.enqueue(second)

        self.assertTrue(first.granted)
        self.assertTrue(second.granted)
        self.assertEqual(gate.pending, 0)

        gate.enqueue(third)
        self.assertFalse(third.granted)
        self.assertEqual(gate.pending, 1)

        gate.release()
        self.assertTrue(third.granted)
        self.assertEqual(gate.pending, 0)

    def test_equal_priority_waiters_keep_fifo_order(self) -> None:
        gate = _gate(capacity=1)
        blocker = gate.new_ticket("current")
        gate.enqueue(blocker)
        first = gate.new_ticket("visible")
        second = gate.new_ticket("visible")
        third = gate.new_ticket("visible")
        for ticket in (first, second, third):
            gate.enqueue(ticket)

        gate.release()
        self.assertTrue(first.granted)
        self.assertFalse(second.granted)
        self.assertFalse(third.granted)

        gate.release()
        self.assertTrue(second.granted)
        self.assertFalse(third.granted)

        gate.release()
        self.assertTrue(third.granted)

    def test_higher_priority_waiter_jumps_ahead_of_earlier_lower_priority(
        self,
    ) -> None:
        gate = _gate(capacity=1)
        gate.enqueue(gate.new_ticket("current"))
        low = gate.new_ticket("background")
        high = gate.new_ticket("current")
        gate.enqueue(low)
        gate.enqueue(high)

        gate.release()
        self.assertTrue(high.granted)
        self.assertFalse(low.granted)

        gate.release()
        self.assertTrue(low.granted)

    def test_oldest_waiter_gets_a_fairness_turn_after_eight_priority_jumps(
        self,
    ) -> None:
        limit = 8
        gate = _gate(capacity=1, max_pending=limit + 4, fairness_limit=limit)
        blocker = gate.new_ticket("current")
        gate.enqueue(blocker)
        oldest = gate.new_ticket("background")
        gate.enqueue(oldest)
        highs = [gate.new_ticket("current") for _ in range(limit + 1)]
        for ticket in highs:
            gate.enqueue(ticket)

        granted: list[RenderTicket] = []
        seen: set[int] = set()

        def snapshot() -> None:
            for ticket in [blocker, oldest, *highs]:
                if ticket.granted and id(ticket) not in seen:
                    seen.add(id(ticket))
                    granted.append(ticket)

        snapshot()
        for _ in range(limit + 1):
            gate.release()
            snapshot()

        self.assertEqual(granted, [blocker, *highs[:limit], oldest])
        self.assertFalse(highs[limit].granted)
        gate.release()
        self.assertTrue(highs[limit].granted)

    def test_full_wait_queue_is_rejected_until_a_slot_frees(self) -> None:
        gate = _gate(capacity=1, max_pending=2)
        gate.enqueue(gate.new_ticket("current"))
        first = gate.new_ticket("visible")
        second = gate.new_ticket("visible")
        gate.enqueue(first)
        gate.enqueue(second)
        self.assertEqual(gate.pending, 2)

        with self.assertRaises(PriorityGateFull):
            gate.enqueue(gate.new_ticket("visible"))

        gate.release()
        self.assertTrue(first.granted)
        self.assertEqual(gate.pending, 1)
        gate.enqueue(gate.new_ticket("visible"))
        self.assertEqual(gate.pending, 2)

    def test_upgrade_promotes_a_ticket_before_and_after_it_is_queued(self) -> None:
        before = _gate(capacity=1)
        before.enqueue(before.new_ticket("current"))
        queued_low = before.new_ticket("background")
        before.enqueue(queued_low)
        not_yet = before.new_ticket("prefetch")
        before.upgrade(not_yet, "current")
        before.enqueue(not_yet)
        before.release()
        self.assertTrue(not_yet.granted)
        self.assertFalse(queued_low.granted)

        after = _gate(capacity=1)
        after.enqueue(after.new_ticket("current"))
        early_low = after.new_ticket("background")
        after.enqueue(early_low)
        late = after.new_ticket("prefetch")
        after.enqueue(late)
        after.upgrade(late, "current")
        after.release()
        self.assertTrue(late.granted)
        self.assertFalse(early_low.granted)

    def test_upgrade_never_recalls_a_granted_slot(self) -> None:
        gate = _gate(capacity=1)
        running = gate.new_ticket("current")
        gate.enqueue(running)
        waiting = gate.new_ticket("current")
        gate.enqueue(waiting)

        gate.upgrade(running, "current")
        self.assertTrue(running.granted)
        self.assertFalse(waiting.granted)

    def test_close_wakes_queued_waiters_and_rejects_new_tickets(self) -> None:
        gate = _gate(capacity=1)
        running = gate.new_ticket("current")
        gate.enqueue(running)
        waiter = gate.new_ticket("visible")
        gate.enqueue(waiter)

        gate.close()

        with self.assertRaises(PriorityGateClosed):
            gate.wait(waiter)
        with self.assertRaises(PriorityGateClosed):
            gate.enqueue(gate.new_ticket("current"))

        # A slot already granted before the close is not revoked.
        gate.wait(running)
        gate.release()
        self.assertFalse(waiter.granted)

    def test_invalid_limits_are_rejected(self) -> None:
        for capacity, max_pending, fairness_limit in (
            (0, 4, 8),
            (1, 0, 8),
            (1, 4, 0),
            (True, 4, 8),
        ):
            with (
                self.subTest(
                    capacity=capacity,
                    max_pending=max_pending,
                    fairness_limit=fairness_limit,
                ),
                self.assertRaises(ValueError),
            ):
                PriorityGate(
                    capacity,
                    max_pending,
                    fairness_limit=fairness_limit,
                )

    def test_unknown_priority_is_rejected_when_creating_a_ticket(self) -> None:
        gate = _gate()
        with self.assertRaises(ValueError):
            gate.new_ticket("urgent")


if __name__ == "__main__":
    unittest.main()
