"""The chat event bus: ordered delivery, non-blocking observers, isolated failures."""
import asyncio
import unittest

from hyadex.core.event_bus import EventBus


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_is_ordered_and_a_failing_subscriber_does_not_stop_others(self):
        bus, seen = EventBus(), []

        @bus.on_deliver
        async def first(session_id, event):
            await asyncio.sleep(0)
            seen.append(("first", event["seq"]))

        @bus.on_deliver
        async def broken(session_id, event):
            raise RuntimeError("socket closed")

        @bus.on_deliver
        async def last(session_id, event):
            seen.append(("last", event["seq"]))

        for seq in (1, 2):
            await bus.deliver("chat", {"seq": seq, "type": "assistant_text"})
        self.assertEqual(seen, [("first", 1), ("last", 1), ("first", 2), ("last", 2)])

    async def test_observers_filter_by_type_and_failures_are_contained(self):
        bus, seen = EventBus(), []
        bus.on_observe(lambda session_id, event: seen.append(event["type"]), types={"turn_finished"})
        bus.on_observe(lambda session_id, event: 1 / 0)
        bus.observe("chat", {"type": "assistant_text"})
        bus.observe("chat", {"type": "turn_finished"})
        self.assertEqual(seen, ["turn_finished"])

    async def test_cancellation_is_not_swallowed(self):
        bus = EventBus()

        @bus.on_deliver
        async def cancelled(session_id, event):
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await bus.deliver("chat", {"seq": 1})


if __name__ == "__main__":
    unittest.main()
