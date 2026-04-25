"""Unit tests for proxy.py pure helpers.

Run inside the proxy container:
    docker compose run --rm proxy python -m unittest test_proxy.py
"""

import json
import os
import unittest

# proxy.main() runs only when invoked as __main__, but module-level import
# touches env defaults. Set required vars before import so module load doesn't
# trigger sys.exit in any future tightened validation path.
os.environ.setdefault("PROXY_API_URL", "http://example.invalid")
os.environ.setdefault("PROXY_API_KEY", "test-key-1234")
os.environ.setdefault("PROXY_API_MODEL", "test-model")

import proxy  # noqa: E402


class TestFormatTools(unittest.TestCase):
    def test_empty_array_returns_no_tools(self):
        self.assertEqual(proxy.format_tools_to_text([]), "No tools available.")

    def test_required_and_optional_labels(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "City name"},
                        "units": {"type": "string", "description": "Units"},
                    },
                    "required": ["city"],
                },
            },
        }]
        out = proxy.format_tools_to_text(tools)
        self.assertIn("get_weather", out)
        self.assertIn("**REQUIRED**", out)
        self.assertIn("Optional", out)
        self.assertIn("`city`", out)
        self.assertIn("`units`", out)


class TestExtractToolCall(unittest.TestCase):
    def test_no_block_returns_none(self):
        text = "Just a normal answer with no JSON block."
        payload, clean = proxy.extract_tool_call_and_text(text)
        self.assertIsNone(payload)
        self.assertEqual(clean, text)

    def test_valid_block_extracted_and_removed(self):
        text = 'Here is a tool call:\n```json\n{"name": "get_weather", "arguments": {"city": "Atlanta"}}\n```\nDone.'
        payload, clean = proxy.extract_tool_call_and_text(text)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["name"], "get_weather")
        self.assertEqual(payload["arguments"], {"city": "Atlanta"})
        self.assertNotIn("```json", clean)
        self.assertIn("Here is a tool call:", clean)
        self.assertIn("Done.", clean)

    def test_malformed_json_returns_none(self):
        text = "```json\n{not valid json}\n```"
        payload, clean = proxy.extract_tool_call_and_text(text)
        self.assertIsNone(payload)
        # The block isn't stripped on malformed JSON.
        self.assertIn("```json", clean)


class TestTranslateHistory(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(proxy.translate_history_and_apply_prompt([], ""), [])

    def test_system_plus_user_with_tools_wraps_final_user(self):
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What's the weather?"},
        ]
        tools_text = "Tool Name: `get_weather`"
        out = proxy.translate_history_and_apply_prompt(msgs, tools_text)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], {"role": "system", "content": "You are helpful."})
        self.assertEqual(out[1]["role"], "user")
        self.assertIn("### Tool Usage Instructions", out[1]["content"])
        self.assertIn("What's the weather?", out[1]["content"])
        self.assertIn("get_weather", out[1]["content"])

    def test_no_tools_does_not_wrap(self):
        msgs = [{"role": "user", "content": "hi"}]
        out = proxy.translate_history_and_apply_prompt(msgs, "")
        self.assertEqual(out, [{"role": "user", "content": "hi"}])

    def test_assistant_tool_call_renders_markdown_block(self):
        msgs = [
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": "I'll check.",
                "tool_calls": [{
                    "function": {
                        "name": "get_weather",
                        "arguments": {"city": "Atlanta"},
                    }
                }],
            },
        ]
        out = proxy.translate_history_and_apply_prompt(msgs, "")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["role"], "assistant")
        content = out[1]["content"]
        self.assertIn("```json", content)
        self.assertIn('"name": "get_weather"', content)
        self.assertIn('"arguments":', content)
        self.assertIn("Atlanta", content)

    def test_tool_message_uses_tool_name_and_folds_into_user(self):
        msgs = [
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Atlanta"}}}],
            },
            {"role": "tool", "tool_name": "get_weather", "content": "72F sunny"},
        ]
        out = proxy.translate_history_and_apply_prompt(msgs, "Tool Name: `get_weather`")
        # Final message should be a user-role with System Observation, wrapped
        # in the tool-variant cooperative prompt.
        self.assertEqual(out[-1]["role"], "user")
        c = out[-1]["content"]
        self.assertIn("System Observation", c)
        self.assertIn("get_weather", c)
        self.assertIn("72F sunny", c)
        self.assertIn("multi-step process", c)  # tool variant prompt
        self.assertIn("Latest System Observation", c)


class TestMakeChunk(unittest.TestCase):
    def test_streaming_chunk_no_done_reason(self):
        c = proxy.make_chunk("harness", content="hello", done=False)
        self.assertEqual(c["model"], "harness")
        self.assertEqual(c["message"]["role"], "assistant")
        self.assertEqual(c["message"]["content"], "hello")
        self.assertFalse(c["done"])
        self.assertNotIn("done_reason", c)
        self.assertNotIn("eval_count", c)
        # JSON-serializable, single-line when dumped without indent.
        line = json.dumps(c)
        self.assertNotIn("\n", line)

    def test_done_chunk_includes_stats(self):
        c = proxy.make_chunk(
            "harness",
            content="",
            done=True,
            done_reason="stop",
            usage={"prompt_tokens": 42, "completion_tokens": 7},
        )
        self.assertTrue(c["done"])
        self.assertEqual(c["done_reason"], "stop")
        self.assertEqual(c["prompt_eval_count"], 42)
        self.assertEqual(c["eval_count"], 7)
        self.assertIn("total_duration", c)
        self.assertIn("load_duration", c)
        self.assertIn("eval_duration", c)


if __name__ == "__main__":
    unittest.main()
