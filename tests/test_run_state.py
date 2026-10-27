"""Turn lifecycle state: one owner, queries with one meaning."""
from collections import deque
import unittest

from hyadex.runs import LEGACY_NAMES, NamespaceRuns, RunState


class RunStateTests(unittest.TestCase):
    def test_holds_turn_covers_every_stage_of_a_turn(self):
        for attribute in ("active", "current_turns"):
            runs = RunState()
            getattr(runs, attribute)["chat"] = {"run_id": "run"}
            self.assertTrue(runs.holds_turn("chat"), attribute)
        runs = RunState()
        runs.busy.add("chat")
        self.assertTrue(runs.holds_turn("chat"))
        self.assertFalse(RunState().holds_turn("chat"))

    def test_waiting_work_ignores_empty_queues(self):
        runs = RunState()
        runs.queued["chat"] = deque()
        self.assertFalse(runs.has_waiting_work("chat"))
        runs.queued["chat"].append({"queued_id": "q"})
        self.assertTrue(runs.has_waiting_work("chat"))
        for setup in (lambda r: r.run_now.update(chat={"queued_id": "q"}), lambda r: r.steering.add("chat")):
            runs = RunState()
            setup(runs)
            self.assertTrue(runs.has_waiting_work("chat"))

    def test_namespace_view_reads_through_and_supports_queries(self):
        namespace = {name: (set() if name in {"BUSY_SESSIONS"} else {}) for name in LEGACY_NAMES}
        view = NamespaceRuns(namespace)
        namespace["ACTIVE"] = {"chat": {"run_id": "replaced"}}
        self.assertIs(view.active, namespace["ACTIVE"])
        self.assertTrue(view.holds_turn("chat"))
        view.busy = {"other"}
        self.assertEqual(namespace["BUSY_SESSIONS"], {"other"})

    def test_every_field_has_a_legacy_name(self):
        fields = set(RunState.__dataclass_fields__)
        self.assertEqual(fields, set(LEGACY_NAMES.values()))


if __name__ == "__main__":
    unittest.main()
