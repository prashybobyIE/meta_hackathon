"""Public planning telemetry reflects actual call boundaries and bounded fallback."""
import json
import unittest

from agent_marketplace.planner import plan_with_meta, reference_plan
from agent_marketplace.protocol import ApiError


class PlannerTraceTests(unittest.TestCase):
    def test_call_boundaries_and_build_dependency_instruction(self):
        events, seen = [], []
        class Provider:
            name, model, max_completion_tokens = "simulated-meta", "muse-spark-1.3", 100
            def complete(self, payload):
                seen.append(payload)
                plan = {"tasks": [{"kind": k, **v} for k, v in reference_plan()["tasks"].items()]}
                return {"choices": [{"message": {"content": json.dumps(plan)}}]}, 0.002
        result = plan_with_meta(Provider(), on_event=lambda event, **fields: events.append({"event": event, **fields}))
        self.assertEqual(result["source"], "simulated-meta")
        self.assertEqual([e["event"] for e in events], ["model_call_started", "model_call_completed"])
        self.assertEqual(events[0]["caller_component"], "planner")
        self.assertEqual(events[1]["cost_usd"], 0.002)
        self.assertIn("CODE-GENERATION", seen[0]["messages"][0]["content"])
        contracts = json.loads(seen[0]["messages"][1]["content"])
        self.assertEqual(contracts["summary"]["required_build_dependencies"], ["parser", "categories"])
        self.assertEqual(contracts["parser"]["required_build_dependencies"], [])
        self.assertEqual(contracts["categories"]["required_build_dependencies"], [])
        self.assertIn("include EVERY listed dependency", seen[0]["messages"][0]["content"])
        self.assertNotIn("hidden_cases", json.dumps(seen))

    def test_failed_call_is_visible_and_reference_fallback_is_explicit(self):
        events = []
        class Provider:
            name, model, max_completion_tokens = "meta", "muse-spark-1.3", 100
            def complete(self, payload):
                raise ApiError(503, "Provider unavailable")
        result = plan_with_meta(Provider(), on_event=lambda event, **fields: events.append({"event": event, **fields}))
        self.assertEqual(result["source"], "reference-fallback")
        self.assertEqual(events[-1]["event"], "model_call_failed")
        self.assertEqual(events[-1]["status"], 503)


if __name__ == "__main__":
    unittest.main()
