import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).parents[1] / "plugins" / "turn-stats" / "hooks" / "turn_stats.py"
SPEC = importlib.util.spec_from_file_location("turn_stats", SCRIPT_PATH)
turn_stats = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(turn_stats)


class TranscriptTestCase(unittest.TestCase):
    def write_transcript(self, entries):
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl", delete=False)
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        with handle:
            for entry in entries:
                if isinstance(entry, str):
                    handle.write(entry)
                else:
                    handle.write(json.dumps(entry) + "\n")
        return handle.name


class MeasureTests(TranscriptTestCase):
    def test_measure_filters_turn_and_collects_usage_generation_and_tools(self):
        path = self.write_transcript(
            [
                {"type": "turn_context", "payload": {"turn_id": "other", "model": "gpt-5.6-sol"}},
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_settings": {"service_tier": "priority"},
                    },
                },
                {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-5.6-luna"}},
                {
                    "type": "token_usage_record",
                    "payload": {
                        "turn_id": "turn-1",
                        "usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 800,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 100,
                            "reasoning_output_tokens": 40,
                        },
                        "turn_token_usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 800,
                            "output_tokens": 100,
                            "reasoning_output_tokens": 40,
                        },
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "turn_id": "other",
                        "type": "item_completed",
                        "started_at_ms": 0,
                        "completed_at_ms": 900,
                        "item": {"type": "Reasoning"},
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "turn_id": "turn-1",
                        "type": "item_completed",
                        "started_at_ms": 1000,
                        "completed_at_ms": 1600,
                        "item": {"type": "Reasoning"},
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "turn_id": "turn-1",
                        "type": "item_completed",
                        "started_at_ms": 1600,
                        "completed_at_ms": 2700,
                        "item": {
                            "type": "CommandExecution",
                            "duration": {"secs": 1, "nanos": 100_000_000},
                        },
                    },
                },
                "not json\n",
            ]
        )

        summary = turn_stats.measure(path, "turn-1")

        self.assertEqual(summary["model"], "gpt-5.6-luna")
        self.assertTrue(summary["fast"])
        self.assertEqual(summary["window_ms"], 600)
        self.assertEqual(summary["tool_calls"], 1)
        self.assertEqual(summary["tool_ms"], 1100)
        self.assertEqual(summary["usage"]["output_tokens"], 100)
        self.assertEqual(len(summary["requests"]), 1)

    def test_measure_keeps_largest_cumulative_usage_and_ignores_malformed_items(self):
        path = self.write_transcript(
            [
                {
                    "type": "token_usage_record",
                    "payload": {
                        "turn_id": "turn-1",
                        "usage": {"output_tokens": 20},
                        "turn_token_usage": {"output_tokens": 20},
                    },
                },
                {
                    "type": "token_usage_record",
                    "payload": {
                        "turn_id": "turn-1",
                        "usage": {"output_tokens": 30},
                        "turn_token_usage": {"output_tokens": 30},
                    },
                },
                {"type": "event_msg", "payload": {"turn_id": "turn-1", "type": "item_completed", "item": "bad"}},
                "{}\n",
            ]
        )

        summary = turn_stats.measure(path, "turn-1")

        self.assertEqual(summary["usage"]["output_tokens"], 30)
        self.assertEqual(len(summary["requests"]), 2)
        self.assertEqual(summary["window_ms"], 0)
        self.assertEqual(summary["tool_calls"], 0)


class CalculationTests(unittest.TestCase):
    def setUp(self):
        self.rates = {
            "input": 1.0,
            "cached_input": 0.5,
            "cache_write": 2.0,
            "output": 3.0,
        }

    def test_request_cost_applies_long_context_multipliers(self):
        usage = {
            "input_tokens": 272_001,
            "cached_input_tokens": 1,
            "cache_write_input_tokens": 1,
            "output_tokens": 2,
        }

        amount = turn_stats.request_cost_usd(usage, self.rates)

        expected = ((271_999 * 1.0 + 1 * 0.5 + 1 * 2.0) * 2.0 + 2 * 3.0 * 1.5) / 1_000_000
        self.assertAlmostEqual(amount, expected)

    def test_rates_for_prefers_longest_matching_model_name(self):
        short = {"gpt-5": {"name": "short"}}
        long = {"gpt-5.6-luna": {"name": "long"}}

        self.assertEqual(
            turn_stats.rates_for("gpt-5.6-luna-2026", {**short, **long})["name"],
            "long",
        )

    def test_format_line_omits_unmeasurable_rate_and_unknown_cost(self):
        summary = {
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 50,
                "output_tokens": 10,
                "reasoning_output_tokens": 4,
            },
            "requests": [{"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10}],
            "window_ms": 100,
            "tool_calls": 0,
            "tool_ms": 0,
            "model": "unknown-model",
            "fast": False,
        }
        show = dict(turn_stats.DEFAULT_SHOW)

        line = turn_stats.format_line(summary, show, 2.0, turn_stats.MODEL_RATES)

        self.assertEqual(line, "10 out (40% reasoning) · CacheHit 50%")

    def test_reasoning_share_stands_alone_when_output_tokens_are_hidden(self):
        summary = {
            "usage": {"output_tokens": 10, "reasoning_output_tokens": 4},
            "requests": [],
            "window_ms": 0,
            "tool_calls": 0,
            "tool_ms": 0,
            "model": None,
            "fast": False,
        }
        show = {key: False for key in turn_stats.DEFAULT_SHOW}
        show["reasoning_share"] = True

        self.assertEqual(turn_stats.format_line(summary, show, 2.0), "reasoning 40%")


class ConfigurationTests(unittest.TestCase):
    def test_load_config_merges_existing_and_new_complete_model_rates(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as handle:
            json.dump(
                {
                    "tps": False,
                    "fast_multiplier": 3,
                    "model_rates": {
                        "GPT-5.6-LUNA": {"output": 9},
                        "custom-model": {
                            "input": 1,
                            "cached_input": 2,
                            "cache_write": 3,
                            "output": 4,
                        },
                    },
                },
                handle,
            )
            handle.flush()
            config = turn_stats.load_config(handle.name)

        self.assertFalse(config["show"]["tps"])
        self.assertEqual(config["fast_multiplier"], 3.0)
        self.assertEqual(config["model_rates"]["gpt-5.6-luna"]["output"], 9.0)
        self.assertEqual(config["model_rates"]["gpt-5.6-luna"]["input"], 0.2)
        self.assertEqual(config["model_rates"]["custom-model"]["output"], 4.0)


class MainProtocolTests(TranscriptTestCase):
    def test_main_emits_json_system_message(self):
        path = self.write_transcript(
            [
                {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-5.6-luna"}},
                {
                    "type": "token_usage_record",
                    "payload": {
                        "turn_id": "turn-1",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                        "turn_token_usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                },
            ]
        )
        output = io.StringIO()

        with patch("sys.stdin", io.StringIO(json.dumps({"transcript_path": path, "turn_id": "turn-1"}))), patch(
            "sys.stdout", output
        ):
            turn_stats.main()

        response = json.loads(output.getvalue())
        self.assertEqual(response.keys(), {"systemMessage"})
        self.assertIn("5 out", response["systemMessage"])

    def test_main_is_silent_without_output_usage(self):
        path = self.write_transcript([])
        output = io.StringIO()

        with patch("sys.stdin", io.StringIO(json.dumps({"transcript_path": path, "turn_id": "turn-1"}))), patch(
            "sys.stdout", output
        ):
            turn_stats.main()

        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
