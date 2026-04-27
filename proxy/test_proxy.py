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

    def test_top_level_schema_emitted(self):
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
        self.assertIn("city", out)
        self.assertIn("units", out)
        # JSON Schema's own required-array marks the required field.
        self.assertIn('"required"', out)
        self.assertIn('"city"', out)

    def test_tool_call_emits_id_field(self):
        """Tool calls in NDJSON output must include an 'id' field that downstream
        Anthropic-compatible agents (claude-code) require for tool_use blocks."""
        chunks = list(proxy.generate_ndjson(
            model_name="test-model",
            clean_text="",
            tool_call_payload={"name": "Bash", "arguments": {"command": "ls"}},
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        ))
        found_tc = False
        for chunk_str in chunks:
            chunk = json.loads(chunk_str)
            msg = chunk.get("message", {})
            tcs = msg.get("tool_calls")
            if tcs:
                found_tc = True
                self.assertEqual(len(tcs), 1)
                tc = tcs[0]
                self.assertIn("id", tc, "tool_call must have 'id' field")
                self.assertTrue(
                    tc["id"].startswith("toolu_"),
                    f"id should start with 'toolu_', got: {tc['id']}",
                )
                self.assertEqual(tc["function"]["name"], "Bash")
        self.assertTrue(found_tc, "expected at least one chunk with tool_calls")

    def test_tool_call_ids_are_unique(self):
        """Two separate tool calls should get different ids."""
        payload = {"name": "Bash", "arguments": {"command": "ls"}}
        chunks1 = list(proxy.generate_ndjson("m", "", payload, {}))
        chunks2 = list(proxy.generate_ndjson("m", "", payload, {}))

        def get_id(chunks):
            for c in chunks:
                msg = json.loads(c).get("message", {})
                tcs = msg.get("tool_calls")
                if tcs:
                    return tcs[0].get("id")
            return None

        id1 = get_id(chunks1)
        id2 = get_id(chunks2)
        self.assertIsNotNone(id1)
        self.assertIsNotNone(id2)
        self.assertNotEqual(id1, id2, "two tool calls should have unique ids")

    def test_format_tools_includes_nested_schema(self):
        """Tools with nested object/array parameters must have full schema in
        the formatted output, not just top-level field names."""
        tools = [{
            "function": {
                "name": "todowrite",
                "description": "Write a todo list",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "todos": {
                            "type": "array",
                            "description": "The updated todo list",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "content": {"type": "string", "description": "Brief description"},
                                    "status": {"type": "string", "description": "Current status"},
                                    "priority": {"type": "string", "description": "Priority level"},
                                },
                                "required": ["content", "status", "priority"],
                            },
                        },
                    },
                    "required": ["todos"],
                },
            },
        }]
        text = proxy.format_tools_to_text(tools)
        self.assertIn("todowrite", text)
        self.assertIn("todos", text)
        self.assertIn("content", text, "nested 'content' field missing from formatted tools")
        self.assertIn("status", text, "nested 'status' field missing from formatted tools")
        self.assertIn("priority", text, "nested 'priority' field missing from formatted tools")
        self.assertIn("required", text, "nested required marker missing")

    def test_format_tools_handles_empty_tools(self):
        """Empty tools list returns the 'No tools available' message."""
        self.assertEqual(proxy.format_tools_to_text([]), "No tools available.")
        self.assertEqual(proxy.format_tools_to_text(None), "No tools available.")


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


class TestExtractToolCallScanner(unittest.TestCase):
    """Tests for the balanced-brace tool call extraction logic.

    The scanner replaced a regex that broke when an LLM emitted a tool
    call whose arguments contained nested code fences (e.g., writing a
    README with embedded ```json examples). These tests cover the failure
    modes that motivated the rewrite plus a few belt-and-braces cases.
    """

    def test_simple_tool_call(self):
        response = '''Here's the call:
```json
{"name": "Read", "arguments": {"path": "foo.txt"}}
```
That's it.'''
        payload, text = proxy.extract_tool_call_and_text(response)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['name'], 'Read')
        self.assertEqual(payload['arguments'], {'path': 'foo.txt'})
        self.assertNotIn('```json', text)

    def test_no_tool_call(self):
        response = "Just a plain text response with no tool call."
        payload, text = proxy.extract_tool_call_and_text(response)
        self.assertIsNone(payload)
        self.assertEqual(text, response)

    def test_tool_call_with_embedded_code_fences_in_arguments(self):
        """Scenario 1: agent writing a markdown file with code fences inside.
        The outer tool-call JSON has a string value containing backticks and
        nested ```json``` blocks. The regex-based extractor would mismatch on
        the inner closing ```. The scanner must navigate past these correctly."""
        response = '''I'll write the README:
```json
{
  "name": "Write",
  "arguments": {
    "file_path": "README.md",
    "content": "# Project\\n\\nExample config:\\n\\n```json\\n{\\"key\\": \\"value\\"}\\n```\\n\\nMore docs follow."
  }
}
```
Done.'''
        payload, text = proxy.extract_tool_call_and_text(response)
        self.assertIsNotNone(payload, "Failed to extract tool call with embedded fences")
        self.assertEqual(payload['name'], 'Write')
        self.assertEqual(payload['arguments']['file_path'], 'README.md')
        self.assertIn('```json', payload['arguments']['content'])
        self.assertIn('"key"', payload['arguments']['content'])

    def test_tool_call_with_braces_in_string_values(self):
        response = '''```json
{"name": "Run", "arguments": {"cmd": "echo {hello} and }nested{ braces"}}
```'''
        payload, _ = proxy.extract_tool_call_and_text(response)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['arguments']['cmd'], 'echo {hello} and }nested{ braces')

    def test_two_blocks_first_invalid(self):
        """Scenario 2: upstream shows a bad example then the real call.
        The scanner must skip the malformed first block and find the second."""
        response = '''Let me consider:
```json
{this is not valid json}
```
But the real call is:
```json
{"name": "Read", "arguments": {"path": "foo.txt"}}
```'''
        payload, _ = proxy.extract_tool_call_and_text(response)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['name'], 'Read')

    def test_two_blocks_first_lacks_required_keys(self):
        """First block parses but isn't a tool call. Scanner should skip it."""
        response = '''```json
{"foo": "bar"}
```
Now the actual call:
```json
{"name": "Read", "arguments": {"path": "foo.txt"}}
```'''
        payload, _ = proxy.extract_tool_call_and_text(response)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['name'], 'Read')

    def test_first_valid_block_wins(self):
        """If the first block IS a valid tool call, use it (don't keep searching)."""
        response = '''```json
{"name": "First", "arguments": {}}
```
And here's another for some reason:
```json
{"name": "Second", "arguments": {}}
```'''
        payload, _ = proxy.extract_tool_call_and_text(response)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['name'], 'First')

    def test_escaped_quotes_in_arguments(self):
        response = '''```json
{"name": "Echo", "arguments": {"text": "She said \\"hi\\" to him"}}
```'''
        payload, _ = proxy.extract_tool_call_and_text(response)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['arguments']['text'], 'She said "hi" to him')

    def test_nested_objects_in_arguments(self):
        response = '''```json
{"name": "Configure", "arguments": {"settings": {"foo": {"bar": 1, "baz": [1, 2, 3]}}, "enabled": true}}
```'''
        payload, _ = proxy.extract_tool_call_and_text(response)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['arguments']['settings']['foo']['bar'], 1)
        self.assertEqual(payload['arguments']['enabled'], True)

    def test_no_closing_fence_but_valid_json(self):
        """Malformed wrapper (no closing ```) but the JSON itself is complete."""
        response = '''```json
{"name": "Read", "arguments": {"path": "foo.txt"}}'''
        payload, _ = proxy.extract_tool_call_and_text(response)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['name'], 'Read')

    def test_truncated_json(self):
        """JSON object opens but never closes (depth never returns to 0)."""
        response = '''```json
{"name": "Read", "arguments": {"path":'''
        payload, text = proxy.extract_tool_call_and_text(response)
        self.assertIsNone(payload)
        self.assertEqual(text, response)

    def test_clean_text_strips_block(self):
        response = '''Before block.
```json
{"name": "Read", "arguments": {"path": "foo.txt"}}
```
After block.'''
        payload, clean = proxy.extract_tool_call_and_text(response)
        self.assertIsNotNone(payload)
        self.assertNotIn('```json', clean)
        self.assertIn('Before block.', clean)
        self.assertIn('After block.', clean)


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

    def test_user_request_uses_marker_delimiters(self):
        """Verify the wrapper uses <<<BEGIN_USER_REQUEST>>> markers, not bare quotes."""
        result = proxy.build_cooperative_prompt_user("hello", "some tools")
        self.assertIn("<<<BEGIN_USER_REQUEST>>>", result)
        self.assertIn("<<<END_USER_REQUEST>>>", result)
        # Explicitly NOT the old quote pattern around content
        self.assertNotIn('"hello"', result)

    def test_user_request_with_complex_content_preserved(self):
        """User content with quotes, code fences, and newlines passes through cleanly."""
        complex_content = '''Please review:
```python
print("hello")
```
And answer "what does this do?"'''
        result = proxy.build_cooperative_prompt_user(complex_content, "tools")
        self.assertIn("<<<BEGIN_USER_REQUEST>>>", result)
        self.assertIn(complex_content, result)
        self.assertIn("<<<END_USER_REQUEST>>>", result)
        self.assertEqual(result.count("<<<BEGIN_USER_REQUEST>>>"), 1)
        self.assertEqual(result.count("<<<END_USER_REQUEST>>>"), 1)

    def test_consecutive_system_messages_are_coalesced(self):
        """Multiple system messages in input → one coalesced system message in output."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "system", "content": "Be concise."},
            {"role": "system", "content": "Do not use emojis."},
            {"role": "user", "content": "Hello"},
        ]
        result = proxy.translate_history_and_apply_prompt(messages, "")
        system_msgs = [m for m in result if m["role"] == "system"]
        self.assertEqual(len(system_msgs), 1, f"Expected 1 system message, got {len(system_msgs)}")
        sys_content = system_msgs[0]["content"]
        self.assertIn("helpful assistant", sys_content)
        self.assertIn("Be concise", sys_content)
        self.assertIn("Do not use emojis", sys_content)

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
