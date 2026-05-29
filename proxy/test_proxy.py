"""Unit tests for proxy.py pure helpers.

Run inside the proxy container:
    docker compose run --rm proxy python -m unittest test_proxy.py
"""

import json
import os
import unittest
from unittest.mock import patch

# proxy.main() runs only when invoked as __main__, but module-level import
# touches env defaults. Set required vars before import so module load doesn't
# trigger sys.exit in any future tightened validation path.
os.environ.setdefault("PROXY_API_URL", "http://example.invalid")
os.environ.setdefault("PROXY_API_KEY", "test-key-1234")
os.environ.setdefault("DEFAULT_MODEL_NAME", "test-model")

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
        Anthropic-format conversation history requires for tool_use blocks."""
        chunks = list(proxy.generate_ndjson(
            model_name="test-model",
            clean_text="",
            tool_call_payloads=[{"name": "Bash", "arguments": {"command": "ls"}}],
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
        payloads = [{"name": "Bash", "arguments": {"command": "ls"}}]
        chunks1 = list(proxy.generate_ndjson("m", "", payloads, {}))
        chunks2 = list(proxy.generate_ndjson("m", "", payloads, {}))

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
    def test_no_block_returns_empty_list(self):
        text = "Just a normal answer with no JSON block."
        payloads, clean = proxy.extract_tool_calls_and_text(text)
        self.assertEqual(payloads, [])
        self.assertEqual(clean, text)

    def test_valid_block_extracted_and_removed(self):
        text = 'Here is a tool call:\n```json\n{"name": "get_weather", "arguments": {"city": "Atlanta"}}\n```\nDone.'
        payloads, clean = proxy.extract_tool_calls_and_text(text)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["name"], "get_weather")
        self.assertEqual(payloads[0]["arguments"], {"city": "Atlanta"})
        self.assertNotIn("```json", clean)
        self.assertIn("Here is a tool call:", clean)
        self.assertIn("Done.", clean)

    def test_malformed_json_returns_empty_list(self):
        text = "```json\n{not valid json}\n```"
        payloads, clean = proxy.extract_tool_calls_and_text(text)
        self.assertEqual(payloads, [])
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
        payloads, text = proxy.extract_tool_calls_and_text(response)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]['name'], 'Read')
        self.assertEqual(payloads[0]['arguments'], {'path': 'foo.txt'})
        self.assertNotIn('```json', text)

    def test_no_tool_call(self):
        response = "Just a plain text response with no tool call."
        payloads, text = proxy.extract_tool_calls_and_text(response)
        self.assertEqual(payloads, [])
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
        payloads, text = proxy.extract_tool_calls_and_text(response)
        self.assertEqual(len(payloads), 1, "Failed to extract tool call with embedded fences")
        self.assertEqual(payloads[0]['name'], 'Write')
        self.assertEqual(payloads[0]['arguments']['file_path'], 'README.md')
        self.assertIn('```json', payloads[0]['arguments']['content'])
        self.assertIn('"key"', payloads[0]['arguments']['content'])

    def test_tool_call_with_braces_in_string_values(self):
        response = '''```json
{"name": "Run", "arguments": {"cmd": "echo {hello} and }nested{ braces"}}
```'''
        payloads, _ = proxy.extract_tool_calls_and_text(response)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]['arguments']['cmd'], 'echo {hello} and }nested{ braces')

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
        payloads, _ = proxy.extract_tool_calls_and_text(response)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]['name'], 'Read')

    def test_two_blocks_first_lacks_required_keys(self):
        """First block parses but isn't a tool call. Scanner should skip it."""
        response = '''```json
{"foo": "bar"}
```
Now the actual call:
```json
{"name": "Read", "arguments": {"path": "foo.txt"}}
```'''
        payloads, _ = proxy.extract_tool_calls_and_text(response)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]['name'], 'Read')

    def test_two_valid_blocks_both_extracted(self):
        """If two valid blocks appear, both are extracted in order."""
        response = '''```json
{"name": "First", "arguments": {}}
```
And here's another:
```json
{"name": "Second", "arguments": {}}
```'''
        payloads, _ = proxy.extract_tool_calls_and_text(response)
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]['name'], 'First')
        self.assertEqual(payloads[1]['name'], 'Second')

    def test_escaped_quotes_in_arguments(self):
        response = '''```json
{"name": "Echo", "arguments": {"text": "She said \\"hi\\" to him"}}
```'''
        payloads, _ = proxy.extract_tool_calls_and_text(response)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]['arguments']['text'], 'She said "hi" to him')

    def test_nested_objects_in_arguments(self):
        response = '''```json
{"name": "Configure", "arguments": {"settings": {"foo": {"bar": 1, "baz": [1, 2, 3]}}, "enabled": true}}
```'''
        payloads, _ = proxy.extract_tool_calls_and_text(response)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]['arguments']['settings']['foo']['bar'], 1)
        self.assertEqual(payloads[0]['arguments']['enabled'], True)

    def test_no_closing_fence_but_valid_json(self):
        """Malformed wrapper (no closing ```) but the JSON itself is complete."""
        response = '''```json
{"name": "Read", "arguments": {"path": "foo.txt"}}'''
        payloads, _ = proxy.extract_tool_calls_and_text(response)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]['name'], 'Read')

    def test_truncated_json(self):
        """JSON object opens but never closes (depth never returns to 0)."""
        response = '''```json
{"name": "Read", "arguments": {"path":'''
        payloads, text = proxy.extract_tool_calls_and_text(response)
        self.assertEqual(payloads, [])
        self.assertEqual(text, response)

    def test_clean_text_strips_block(self):
        response = '''Before block.
```json
{"name": "Read", "arguments": {"path": "foo.txt"}}
```
After block.'''
        payloads, clean = proxy.extract_tool_calls_and_text(response)
        self.assertEqual(len(payloads), 1)
        self.assertNotIn('```json', clean)
        self.assertIn('Before block.', clean)
        self.assertIn('After block.', clean)

    def test_extract_multiple_tool_calls_in_order(self):
        """When the response has multiple ```json blocks, all are extracted
        in their order of appearance."""
        text = (
            "I'll read both files.\n\n"
            "```json\n"
            '{"name": "Read", "arguments": {"file_path": "/a.py"}}\n'
            "```\n\n"
            "```json\n"
            '{"name": "Read", "arguments": {"file_path": "/b.py"}}\n'
            "```\n\n"
            "After that, I'll summarize."
        )
        payloads, clean = proxy.extract_tool_calls_and_text(text)
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["arguments"]["file_path"], "/a.py")
        self.assertEqual(payloads[1]["arguments"]["file_path"], "/b.py")
        # Clean text has both blocks removed but preserves the surrounding text
        self.assertIn("I'll read both files", clean)
        self.assertIn("After that, I'll summarize", clean)
        self.assertNotIn("```json", clean)
        self.assertNotIn("/a.py", clean)
        self.assertNotIn("/b.py", clean)

    def test_extract_no_tool_calls_returns_empty_list(self):
        """Plain text response (no tool calls) returns empty list, not None."""
        text = "Hello! How can I help you today?"
        payloads, clean = proxy.extract_tool_calls_and_text(text)
        self.assertEqual(payloads, [])
        self.assertEqual(clean, text)

    def test_extract_invalid_json_block_left_in_text(self):
        """A ```json block with invalid JSON or wrong shape stays in the text."""
        text = (
            "Here's a JSON example I'm describing:\n"
            "```json\n"
            '{"this is": "not a tool call"}\n'
            "```\n"
            "And here's a real one:\n"
            "```json\n"
            '{"name": "Read", "arguments": {"file_path": "/x.py"}}\n'
            "```"
        )
        payloads, clean = proxy.extract_tool_calls_and_text(text)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["name"], "Read")
        # The invalid block stays in clean text (it's just text, not a tool call)
        self.assertIn("not a tool call", clean)

    def test_three_back_to_back_tool_calls_with_merged_fences(self):
        """When the model emits consecutive tool calls and reuses ```json as
        both the closer of one block and the opener of the next, every block
        must still be extracted and clean_text must be empty."""
        text = (
            "```json\n"
            '{"name": "read", "arguments": {"filePath": "/a"}}\n'
            "```json\n"
            '{"name": "read", "arguments": {"filePath": "/b"}}\n'
            "```json\n"
            '{"name": "read", "arguments": {"filePath": "/c"}}\n'
            "```"
        )
        payloads, clean = proxy.extract_tool_calls_and_text(text)
        self.assertEqual(len(payloads), 3)
        self.assertEqual(payloads[0]["arguments"]["filePath"], "/a")
        self.assertEqual(payloads[1]["arguments"]["filePath"], "/b")
        self.assertEqual(payloads[2]["arguments"]["filePath"], "/c")
        self.assertEqual(clean, "")

    def test_two_normally_fenced_tool_calls_still_work(self):
        """Properly-fenced consecutive tool calls (with real ``` separators)
        continue to extract correctly after the merged-fence fix."""
        text = (
            "```json\n"
            '{"name": "read", "arguments": {"filePath": "/a"}}\n'
            "```\n"
            "```json\n"
            '{"name": "read", "arguments": {"filePath": "/b"}}\n'
            "```"
        )
        payloads, clean = proxy.extract_tool_calls_and_text(text)
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["arguments"]["filePath"], "/a")
        self.assertEqual(payloads[1]["arguments"]["filePath"], "/b")
        self.assertEqual(clean, "")

    def test_tool_call_then_prose_then_tool_call(self):
        """Mixed content: tool call, prose, tool call. Both calls extract and
        the prose between them survives in clean_text."""
        text = (
            "```json\n"
            '{"name": "read", "arguments": {"filePath": "/a"}}\n'
            "```\n"
            "Now let me read the second file.\n"
            "```json\n"
            '{"name": "read", "arguments": {"filePath": "/b"}}\n'
            "```"
        )
        payloads, clean = proxy.extract_tool_calls_and_text(text)
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["arguments"]["filePath"], "/a")
        self.assertEqual(payloads[1]["arguments"]["filePath"], "/b")
        self.assertIn("Now let me read the second file.", clean)
        self.assertNotIn("```json", clean)
        self.assertNotIn("/a", clean)
        self.assertNotIn("/b", clean)

    def test_literal_newlines_in_string_value(self):
        """Issue #115 — model emits a multi-line `python -c "..."` as the
        bash `command` value with real `\\n` bytes inside the JSON string
        instead of `\\\\n` escapes. Strict json.loads rejects unescaped
        control characters in strings; we must accept them, otherwise the
        whole fenced block falls through to clean_text and bleeds into
        chat. strict=False is the json.loads flag for this."""
        text = (
            "```json\n"
            "{\n"
            '  "name": "bash",\n'
            '  "arguments": {\n'
            '    "command": "python3 -c \\"\n'
            "import os\n"
            "print(os.getcwd())\n"
            '\\"",\n'
            '    "description": "Print cwd."\n'
            "  }\n"
            "}\n"
            "```"
        )
        payloads, clean = proxy.extract_tool_calls_and_text(text)
        self.assertEqual(len(payloads), 1, "literal-newline JSON must still extract")
        self.assertEqual(payloads[0]["name"], "bash")
        self.assertIn("import os", payloads[0]["arguments"]["command"])
        self.assertEqual(payloads[0]["arguments"]["description"], "Print cwd.")
        self.assertNotIn("```json", clean)
        self.assertNotIn("import os", clean)

    def test_literal_tab_in_string_value(self):
        """Tabs (U+0009) are also control characters that strict json.loads
        rejects inside strings. Same fix covers them."""
        text = (
            "```json\n"
            '{"name": "bash", "arguments": {"command": "echo a\tb"}}\n'
            "```"
        )
        payloads, _ = proxy.extract_tool_calls_and_text(text)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["arguments"]["command"], "echo a\tb")

    def test_generate_ndjson_emits_multiple_tool_calls(self):
        """Multiple tool calls produce a single tool_calls array in one chunk,
        each with a unique id."""
        payloads = [
            {"name": "Read", "arguments": {"file_path": "/a.py"}},
            {"name": "Read", "arguments": {"file_path": "/b.py"}},
            {"name": "Bash", "arguments": {"command": "ls"}},
        ]
        chunks = list(proxy.generate_ndjson("test-model", "", payloads, {}))

        # Find the chunk with tool_calls
        found_tc_chunk = None
        for chunk_str in chunks:
            chunk = json.loads(chunk_str)
            msg = chunk.get("message", {})
            tcs = msg.get("tool_calls")
            if tcs:
                found_tc_chunk = tcs

        self.assertIsNotNone(found_tc_chunk)
        self.assertEqual(len(found_tc_chunk), 3)

        # Each call has its own unique id
        ids = [tc["id"] for tc in found_tc_chunk]
        self.assertEqual(len(set(ids)), 3, "ids should be unique")
        for tc_id in ids:
            self.assertTrue(tc_id.startswith("toolu_"))

        # Order preserved
        self.assertEqual(found_tc_chunk[0]["function"]["arguments"]["file_path"], "/a.py")
        self.assertEqual(found_tc_chunk[1]["function"]["arguments"]["file_path"], "/b.py")
        self.assertEqual(found_tc_chunk[2]["function"]["name"], "Bash")


class TestTranslateHistory(unittest.TestCase):
    def setUp(self):
        # These tests assert on the legacy role layout (system role passes
        # through). Disable the new system→user conversion for the duration.
        # The conversion has its own dedicated test class below.
        p = patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", False)
        p.start()
        self.addCleanup(p.stop)

    def test_empty_returns_empty(self):
        self.assertEqual(proxy.translate_history_and_apply_prompt([], ""), [])

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
        result = proxy.build_cooperative_prompt_user_front("hello", "some tools")
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
        result = proxy.build_cooperative_prompt_user_front(complex_content, "tools")
        self.assertIn("<<<BEGIN_USER_REQUEST>>>", result)
        self.assertIn(complex_content, result)
        self.assertIn("<<<END_USER_REQUEST>>>", result)
        self.assertEqual(result.count("<<<BEGIN_USER_REQUEST>>>"), 1)
        self.assertEqual(result.count("<<<END_USER_REQUEST>>>"), 1)

    def test_user_front_builder_omits_generic_persona_line(self):
        """The user_front builder must NOT re-declare a generic persona
        ("You are a helpful and intelligent AI assistant.").  That line
        lands in the last user message every turn, so the model treated
        it as the active persona and collapsed the real persona from the
        upstream conversation. The intro line instead introduces the
        user-request block and tells the model to keep its established
        identity — it must not describe the tool-call format (the tool
        scaffolding sits after the request, not after the intro)."""
        out = proxy.build_cooperative_prompt_user_front("do the thing", "TOOLS_HERE")
        self.assertNotIn("helpful and intelligent AI assistant", out)
        self.assertIn("do not adopt a new identity", out)
        self.assertIn("user's next message", out)
        self.assertNotIn("tool-call format", out)

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

    def test_tool_message_uses_tool_name_and_wraps_in_markers(self):
        # The translator wraps every role:"tool" message in
        # <<<BEGIN_TOOL_RESULT>>> / <<<END_TOOL_RESULT>>> markers. In
        # user_front mode the tool-variant builder then injects the framing
        # line and tool list around that already-delimited block.
        msgs = [
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Atlanta"}}}],
            },
            {"role": "tool", "tool_name": "get_weather", "content": "72F sunny"},
        ]
        with patch.object(proxy, "_PROMPT_MODE", "user_front"):
            out = proxy.translate_history_and_apply_prompt(msgs, "Tool Name: `get_weather`")
        self.assertEqual(out[-1]["role"], "user")
        c = out[-1]["content"]
        # Tool result wrapped in explicit open/close markers; name from metadata.
        self.assertIn('<<<BEGIN_TOOL_RESULT name="get_weather">>>', c)
        self.assertIn("<<<END_TOOL_RESULT>>>", c)
        self.assertIn("72F sunny", c)
        # Framing line present; the old "System Observation" vocabulary is gone.
        self.assertIn("NOT a message from the user", c)
        self.assertNotIn("System Observation", c)
        # Tool-variant builder still injects the tool list.
        self.assertIn("get_weather", c)


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


class TestPromptInjectionModes(unittest.TestCase):
    """Two configurable injection paths for the cooperative tool-use
    scaffolding, selected by PROXY_PROMPT_MODE: 'user_front', 'hybrid'.

    Each test patches `proxy._PROMPT_MODE` and runs the translation. The
    fully-formatted tool text comes from `format_tools_to_text`; tests
    assert on stable substrings (tool name, description, mode-specific
    markers) rather than the entire scaffold.
    """

    def setUp(self):
        # These tests assert on the legacy role layout (system role passes
        # through). The new system→user conversion is covered by
        # TestChangeSystemToUser below.
        p = patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", False)
        p.start()
        self.addCleanup(p.stop)

        self.user_msgs = [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "say hello"},
        ]
        self.tools = [
            {"function": {
                "name": "Bash",
                "description": "Run shell command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            }},
        ]

    def _translate_with_mode(self, mode, pass_tools=False):
        tools_text = proxy.format_tools_to_text(self.tools)
        kwargs = {"tools": self.tools} if pass_tools else {}
        with patch.object(proxy, "_PROMPT_MODE", mode):
            return proxy.translate_history_and_apply_prompt(
                self.user_msgs, tools_text, **kwargs,
            )

    def test_mode_hybrid_full_tools_in_system_reminder_in_user(self):
        result = self._translate_with_mode("hybrid", pass_tools=True)
        # [0] is the system message; it must contain the original system
        # text AND the full tool definitions (Tool Name / JSON Schema).
        sys_content = result[0]["content"]
        self.assertIn("You are a coding agent.", sys_content)
        self.assertIn("Tool Name: `Bash`", sys_content)
        self.assertIn("Run shell command", sys_content)
        self.assertIn('"required"', sys_content, "JSON Schema body present in system")
        self.assertIn('"command"', sys_content)

        # The inbound agent system prompt is delimited in AGENT_INSTRUCTIONS,
        # and the harness tool block in AGENT_TOOLS. The legacy
        # "### Available Tools" markdown header is gone — the marker plus the
        # disambiguation sentence replaces it.
        self.assertIn("<<<BEGIN_AGENT_INSTRUCTIONS>>>", sys_content)
        self.assertIn("<<<END_AGENT_INSTRUCTIONS>>>", sys_content)
        self.assertIn("<<<BEGIN_AGENT_TOOLS>>>", sys_content)
        self.assertIn("<<<END_AGENT_TOOLS>>>", sys_content)
        self.assertNotIn("### Available Tools", sys_content)
        # AGENT_INSTRUCTIONS wraps the original system content.
        self.assertLess(
            sys_content.index("<<<BEGIN_AGENT_INSTRUCTIONS>>>"),
            sys_content.index("You are a coding agent."),
        )
        self.assertLess(
            sys_content.index("You are a coding agent."),
            sys_content.index("<<<END_AGENT_INSTRUCTIONS>>>"),
        )
        # The disambiguation sentence and the tool definitions both live
        # inside the AGENT_TOOLS wrap.
        tools_open = sys_content.index("<<<BEGIN_AGENT_TOOLS>>>")
        tools_close = sys_content.index("<<<END_AGENT_TOOLS>>>")
        disambig = sys_content.index("only tools available for use in this conversation")
        bash_def = sys_content.index("Tool Name: `Bash`")
        self.assertTrue(tools_open < disambig < tools_close)
        self.assertTrue(tools_open < bash_def < tools_close)

        last_user = result[-1]["content"]
        # The new reminder is present.
        self.assertIn("Reminder", last_user)
        # Each tool gets a recency entry led by its signature (parameter
        # keys, not just the bare name — the chronic miss).
        self.assertIn("- Bash(command)", last_user)
        # Signature-format legend telling the model the listed keys are exact.
        self.assertIn("parameter names must match exactly", last_user)
        # New sentence telling the model not to fabricate tool results.
        self.assertIn("do not invent", last_user)
        # The user's live request is wrapped in USER_REQUEST on the last
        # turn (not USER_MESSAGE — USER_MESSAGE wraps prior user turns).
        self.assertIn("say hello", last_user)
        self.assertIn("<<<BEGIN_USER_REQUEST>>>", last_user)
        self.assertIn("<<<END_USER_REQUEST>>>", last_user)
        self.assertNotIn("<<<BEGIN_USER_MESSAGE>>>", last_user)
        # USER_REQUEST sits at the FRONT — the live ask comes BEFORE the
        # reminder so the model's most-recent attention lands on the
        # user's actual question, not on operating-rules prose.
        self.assertLess(
            last_user.index("<<<BEGIN_USER_REQUEST>>>"),
            last_user.index("Reminder"),
        )
        # The reminder must NOT contain the full tool schema or instructions
        # header — those live only in the prefix at [0].
        self.assertNotIn("Tool Usage Instructions", last_user)
        self.assertNotIn("Run shell command", last_user)
        self.assertNotIn('"required"', last_user)

    def test_mode_hybrid_reminder_advises_default_to_tools(self):
        """The Operating bullet nudges the model to reach for a dedicated tool
        rather than improvising by hand, and to track work with `todowrite`."""
        result = self._translate_with_mode("hybrid", pass_tools=True)
        last_user = result[-1]["content"]
        self.assertIn("prefer a listed tool over doing the work by hand", last_user)
        # Concrete examples the user asked for.
        self.assertIn("webfetch", last_user)
        self.assertIn("todowrite", last_user)
        # The guidance is part of the reminder (proxy stage-direction),
        # which now sits AFTER the wrapped user request.
        self.assertGreater(
            last_user.index("prefer a listed tool over doing the work by hand"),
            last_user.index("<<<END_USER_REQUEST>>>"),
        )

    def test_mode_hybrid_reminder_advises_concurrent_task_agents(self):
        """The Operating bullet tells the model to launch task agents, several
        concurrently when possible, to parallelize and conserve context."""
        result = self._translate_with_mode("hybrid", pass_tools=True)
        last_user = result[-1]["content"]
        self.assertIn("Launch `task` agents", last_user)
        self.assertIn("concurrently", last_user)
        self.assertIn("conserve your context", last_user)

    def test_mode_hybrid_reminder_has_honesty_rules(self):
        """The Honesty bullet carries the anti-fabrication guidance."""
        result = self._translate_with_mode("hybrid", pass_tools=True)
        last_user = result[-1]["content"]
        self.assertIn("never fabricate", last_user)
        self.assertIn("Do not present guesses as facts", last_user)
        self.assertIn("I don't know", last_user)

    def test_mode_hybrid_reminder_has_agency_assertion(self):
        """The Operating bullet asserts that tool calls really execute against
        the working directory mounted from the user's machine and results are
        real — the missing anchor for issue #109's reversion to "I can't
        execute, here are commands you should run" right after a tool call."""
        result = self._translate_with_mode("hybrid", pass_tools=True)
        last_user = result[-1]["content"]
        self.assertIn("Operating:", last_user)
        self.assertIn("really execute", last_user)
        self.assertIn("results you get back are real", last_user)
        # Don't downgrade to handing the user commands.
        self.assertIn("don't downgrade to listing commands", last_user)
        # The legitimate fallback (when no tool fits) stays available.
        self.assertIn("just ask or answer", last_user)

    def test_mode_hybrid_reminder_agency_names_opencode(self):
        """The Operating bullet names opencode as the disambiguator from the
        upstream's own (phantom) tools/subagents — bare "agency"/"tools"
        could otherwise re-resolve to the upstream's persona."""
        result = self._translate_with_mode("hybrid", pass_tools=True)
        last_user = result[-1]["content"]
        self.assertIn("opencode", last_user)
        # Specifically the opencode tools (not just "tools") are the
        # referent for what to call.
        self.assertIn("opencode tools", last_user)

    def test_mode_hybrid_reminder_operating_points_at_agent_tools(self):
        """The Operating bullet tells the model the tools are listed below
        in this reminder AND in <<<BEGIN_AGENT_TOOLS>>> earlier in the
        conversation (authoritative copy that may have drifted out of
        attention on long conversations)."""
        result = self._translate_with_mode("hybrid", pass_tools=True)
        last_user = result[-1]["content"]
        operating_start = last_user.index("- Operating:")
        operating_block = last_user[
            operating_start:last_user.index("- Honesty:", operating_start)
        ]
        self.assertIn("<<<BEGIN_AGENT_TOOLS>>>", operating_block)
        # "listed below" pointer to the per-tool entries that follow.
        self.assertIn("below", operating_block)

    def test_mode_hybrid_reminder_operating_is_first_label(self):
        """Operating is the merged Agency/Tools/Workflow bullet and is the
        premise for everything else, so it sits first — before Honesty and
        Environment. Tools/Workflow/Agency labels no longer exist as
        separate lines."""
        result = self._translate_with_mode("hybrid", pass_tools=True)
        last_user = result[-1]["content"]
        self.assertLess(last_user.index("- Operating:"), last_user.index("- Honesty:"))
        self.assertLess(last_user.index("- Operating:"), last_user.index("- Environment:"))
        # The old separate labels are gone.
        self.assertNotIn("- Agency:", last_user)
        self.assertNotIn("- Tools:", last_user)
        self.assertNotIn("- Workflow:", last_user)

    def test_mode_hybrid_reminder_lives_after_user_request(self):
        """The reminder is proxy stage-direction; with the user-request-first
        ordering it sits AFTER the wrapped USER_REQUEST block (the user's
        actual ask now comes first; the reminder follows)."""
        result = self._translate_with_mode("hybrid", pass_tools=True)
        last_user = result[-1]["content"]
        self.assertLess(
            last_user.index("<<<END_USER_REQUEST>>>"),
            last_user.index("Operating:"),
        )

    def test_mode_hybrid_reminder_has_environment_context(self):
        """The Environment line states the container / mounted-workdir /
        reproducibility facts so agents give reproducible setup advice."""
        result = self._translate_with_mode("hybrid", pass_tools=True)
        last_user = result[-1]["content"]
        self.assertIn("Linux container", last_user)
        self.assertIn("mounted from the host", last_user)
        self.assertIn("reproduce in the user's environment", last_user)
        self.assertIn("project-local", last_user)

    def test_mode_hybrid_reminder_drops_stale_value_parenthetical(self):
        """The old "which agent types are valid for a `task` tool" pointer is
        gone — closed-set values (a `task`'s agent types, a `skill`'s names)
        now live INLINED under each tool's own entry, so pointing back to
        AGENT_TOOLS for them would misdirect."""
        result = self._translate_with_mode("hybrid", pass_tools=True)
        last_user = result[-1]["content"]
        self.assertNotIn("which agent types are valid for a `task` tool", last_user)
        self.assertNotIn("which skills are listed for a `skill` tool", last_user)

    def test_mode_hybrid_reminder_injects_known_host_os(self):
        """When HARNESS_HOST_OS is a recognised value the Environment line
        names it; the rest of the line is unchanged."""
        with patch.object(proxy, "_HOST_OS", "macos"):
            result = self._translate_with_mode("hybrid", pass_tools=True)
        last_user = result[-1]["content"]
        self.assertIn("host OS: macos", last_user)
        self.assertIn("Linux container", last_user)

    def test_mode_hybrid_reminder_omits_host_os_when_unknown(self):
        """An empty/unknown host OS suppresses only the parenthetical; the
        container/reproducibility facts still render."""
        with patch.object(proxy, "_HOST_OS", ""):
            result = self._translate_with_mode("hybrid", pass_tools=True)
        last_user = result[-1]["content"]
        self.assertNotIn("host OS:", last_user)
        self.assertIn("mounted from the host.", last_user)

    def test_mode_hybrid_signatures_show_required_and_optional(self):
        """The reminder lists required params bare and each optional param
        in its own `[brackets]`. This is the recency anchor for the keys
        the model most often guesses wrong (e.g. opencode's `bash` requires
        both `command` and `description`; `read` takes `filePath` and
        optional `offset`/`limit`)."""
        tools = [
            {"function": {
                "name": "bash",
                "description": "Run a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "description": {"type": "string"},
                        "timeout": {"type": "integer"},
                        "workdir": {"type": "string"},
                    },
                    "required": ["command", "description"],
                },
            }},
            {"function": {
                "name": "read",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filePath": {"type": "string"},
                        "offset": {"type": "integer"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["filePath"],
                },
            }},
            {"function": {
                "name": "noop",
                "description": "no params",
                "parameters": {"type": "object", "properties": {}},
            }},
        ]
        tools_text = proxy.format_tools_to_text(tools)
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            result = proxy.translate_history_and_apply_prompt(
                self.user_msgs, tools_text, tools=tools,
            )
        last_user = result[-1]["content"]
        self.assertIn("bash(command, description, [timeout], [workdir])", last_user)
        self.assertIn("read(filePath, [offset], [limit])", last_user)
        # Zero-param tool: bare name, no empty parens.
        self.assertIn(" noop", last_user)
        self.assertNotIn("noop()", last_user)

    def test_mode_hybrid_signatures_via_tools_text_fallback(self):
        """When the production `tools=` kwarg is omitted (test convenience
        path), signatures are parsed from the schema blocks embedded in
        `tools_text` by `format_tools_to_text`. Same output shape as the
        primary path."""
        result = self._translate_with_mode("hybrid")  # no pass_tools
        last_user = result[-1]["content"]
        self.assertIn("Bash(command)", last_user)

    def test_mode_user_front_request_before_tools(self):
        """In user_front mode, the user's request appears BEFORE the tool
        list, wrapped in <<<BEGIN_USER_REQUEST>>> markers, so the model
        sees the actual question in the primacy position rather than buried
        after 10-15K tokens of tool schemas."""
        result = self._translate_with_mode("user_front")
        last_user = result[-1]["content"]

        self.assertIn("<<<BEGIN_USER_REQUEST>>>", last_user)
        self.assertIn("<<<END_USER_REQUEST>>>", last_user)
        self.assertIn("say hello", last_user)

        request_marker_pos = last_user.index("<<<BEGIN_USER_REQUEST>>>")
        tools_section_pos = last_user.index("Available Tools")
        self.assertLess(
            request_marker_pos, tools_section_pos,
            "user_front: request should appear before tools, not after",
        )

        self.assertIn("Bash", last_user)
        self.assertIn("Run shell command", last_user)

    def test_invalid_mode_falls_back_to_hybrid(self):
        with patch.dict(os.environ, {"PROXY_PROMPT_MODE": "garbage"}):
            proxy._setup_prompt_mode()
            self.assertEqual(proxy._PROMPT_MODE, "hybrid")
        # PROXY_PROMPT_MODE is no longer a user .env knob, but the proxy still
        # honors it from its container env (injected by `harness --prompt-mode`)
        # for benchmarking. A stale or removed value must fall back to the new
        # default, hybrid — including the legacy "user" value a stale .env might
        # still carry. This guards against silent re-introduction.
        for removed in ("user", "system", "user_bookend"):
            with patch.dict(os.environ, {"PROXY_PROMPT_MODE": removed}):
                proxy._setup_prompt_mode()
                self.assertEqual(
                    proxy._PROMPT_MODE, "hybrid",
                    f"removed mode '{removed}' should fall back to hybrid",
                )

    def test_default_mode_is_hybrid(self):
        env_no_mode = {k: v for k, v in os.environ.items() if k != "PROXY_PROMPT_MODE"}
        with patch.dict(os.environ, env_no_mode, clear=True):
            proxy._setup_prompt_mode()
            self.assertEqual(proxy._PROMPT_MODE, "hybrid")

    def test_no_tools_skips_injection_in_all_modes(self):
        """No tools defined → no scaffolding regardless of mode."""
        for mode in ("hybrid", "user_front"):
            with patch.object(proxy, "_PROMPT_MODE", mode):
                result = proxy.translate_history_and_apply_prompt(self.user_msgs, "")
            self.assertEqual(result[0]["content"], "You are a coding agent.", mode)
            self.assertEqual(result[-1]["content"], "say hello", mode)

    def test_hybrid_tool_result_message_gets_reminder(self):
        """In hybrid mode, when the last message is a tool-result-converted
        user message, the reminder still applies — the model needs to know
        tools are available regardless of how the user msg formed."""
        msgs = [
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Atlanta"}}}],
            },
            {"role": "tool", "tool_name": "get_weather", "content": "72F sunny"},
        ]
        tools_text = proxy.format_tools_to_text(self.tools)
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            out = proxy.translate_history_and_apply_prompt(msgs, tools_text)
        # Last message is the tool-result-converted user, with both the
        # <<<BEGIN_TOOL_RESULT>>> markers and the hybrid reminder prefix.
        self.assertEqual(out[-1]["role"], "user")
        c = out[-1]["content"]
        # New reminder text ("Reminder" — not the old "Tool reminder").
        self.assertIn("Reminder", c)
        # The new "do not invent" sentence telling the model not to
        # fabricate tool results.
        self.assertIn("do not invent", c)
        self.assertIn('<<<BEGIN_TOOL_RESULT name="get_weather">>>', c)
        self.assertIn("<<<END_TOOL_RESULT>>>", c)
        self.assertIn("72F sunny", c)
        # A tool-result-converted message is NOT a user message, so it must
        # NOT also receive a USER_MESSAGE wrap — only the TOOL_RESULT markers
        # from the universal pre-dispatch wrap, plus the reminder.
        self.assertNotIn("<<<BEGIN_USER_MESSAGE>>>", c)
        self.assertNotIn("<<<END_USER_MESSAGE>>>", c)
        # The full per-turn tool-variant builder framing should NOT be present.
        self.assertNotIn("NOT a message from the user", c)

    def test_mode_hybrid_wraps_system_content_in_agent_instructions(self):
        """Non-trivial inbound system content is delimited in
        AGENT_INSTRUCTIONS markers at messages[0] so the model can tell the
        agent's own system prompt apart from harness's tool block and the
        gateway's system content."""
        result = self._translate_with_mode("hybrid", pass_tools=True)
        sys_content = result[0]["content"]
        self.assertIn("<<<BEGIN_AGENT_INSTRUCTIONS>>>", sys_content)
        self.assertIn("<<<END_AGENT_INSTRUCTIONS>>>", sys_content)
        open_pos = sys_content.index("<<<BEGIN_AGENT_INSTRUCTIONS>>>")
        text_pos = sys_content.index("You are a coding agent.")
        close_pos = sys_content.index("<<<END_AGENT_INSTRUCTIONS>>>")
        self.assertTrue(open_pos < text_pos < close_pos)

    def test_mode_hybrid_no_agent_instructions_wrap_when_no_system_content(self):
        """With no inbound system message, no AGENT_INSTRUCTIONS markers
        appear — but the harness tool block still gets its AGENT_TOOLS wrap on
        the inserted system message."""
        msgs = [{"role": "user", "content": "hi there"}]
        tools_text = proxy.format_tools_to_text(self.tools)
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            result = proxy.translate_history_and_apply_prompt(
                msgs, tools_text, tools=self.tools,
            )
        joined = "\n".join(m["content"] for m in result)
        self.assertNotIn("<<<BEGIN_AGENT_INSTRUCTIONS>>>", joined)
        self.assertNotIn("<<<END_AGENT_INSTRUCTIONS>>>", joined)
        self.assertIn("<<<BEGIN_AGENT_TOOLS>>>", joined)

    def test_mode_hybrid_no_agent_instructions_wrap_when_empty_system_content(self):
        """Empty/whitespace-only inbound system content gets no
        AGENT_INSTRUCTIONS wrap (empty markers would be noise); the AGENT_TOOLS
        wrap is still applied."""
        msgs = [
            {"role": "system", "content": "   \n  "},
            {"role": "user", "content": "hi there"},
        ]
        tools_text = proxy.format_tools_to_text(self.tools)
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            result = proxy.translate_history_and_apply_prompt(
                msgs, tools_text, tools=self.tools,
            )
        joined = "\n".join(m["content"] for m in result)
        self.assertNotIn("<<<BEGIN_AGENT_INSTRUCTIONS>>>", joined)
        self.assertNotIn("<<<END_AGENT_INSTRUCTIONS>>>", joined)
        self.assertIn("<<<BEGIN_AGENT_TOOLS>>>", joined)

    def test_mode_hybrid_wraps_prior_user_turns_in_user_message(self):
        """In a multi-turn conversation, every PRIOR real user-role turn is
        wrapped in USER_MESSAGE markers. The LAST real user turn (the live
        ask) is wrapped in USER_REQUEST by the recency builder instead and
        placed at the front of the recency block."""
        msgs = [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "first turn"},
            {"role": "assistant", "content": "ok one"},
            {"role": "user", "content": "second turn"},
            {"role": "assistant", "content": "ok two"},
            {"role": "user", "content": "third turn"},
        ]
        tools_text = proxy.format_tools_to_text(self.tools)
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            result = proxy.translate_history_and_apply_prompt(
                msgs, tools_text, tools=self.tools,
            )
        user_msgs = [m for m in result if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 3)
        # The first two user turns are historical — wrapped in USER_MESSAGE.
        for m in user_msgs[:-1]:
            self.assertIn("<<<BEGIN_USER_MESSAGE>>>", m["content"])
            self.assertIn("<<<END_USER_MESSAGE>>>", m["content"])
        # The last (live) user turn is wrapped in USER_REQUEST by the
        # recency builder and sits at the front of the recency block.
        live = user_msgs[-1]["content"]
        self.assertIn("<<<BEGIN_USER_REQUEST>>>", live)
        self.assertIn("<<<END_USER_REQUEST>>>", live)
        self.assertNotIn("<<<BEGIN_USER_MESSAGE>>>", live)
        joined = "\n".join(m["content"] for m in result)
        # Two USER_MESSAGE wraps (for the prior two user turns), one
        # USER_REQUEST wrap (for the live one).
        self.assertEqual(joined.count("<<<BEGIN_USER_MESSAGE>>>"), 2)
        self.assertEqual(joined.count("<<<BEGIN_USER_REQUEST>>>"), 1)

    def test_mode_hybrid_does_not_wrap_tool_result_converted_user_messages(self):
        """A real user turn gets a USER_MESSAGE wrap; a tool-result-converted
        user turn (TOOL_RESULT markers) does not."""
        msgs = [
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Atlanta"}}}],
            },
            {"role": "tool", "tool_name": "get_weather", "content": "72F sunny"},
        ]
        tools_text = proxy.format_tools_to_text(self.tools)
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            result = proxy.translate_history_and_apply_prompt(
                msgs, tools_text, tools=self.tools,
            )
        real_user = next(m for m in result if "weather?" in m["content"])
        tool_result = next(m for m in result if "72F sunny" in m["content"])
        self.assertIn("<<<BEGIN_USER_MESSAGE>>>", real_user["content"])
        self.assertNotIn("<<<BEGIN_USER_MESSAGE>>>", tool_result["content"])
        self.assertIn('<<<BEGIN_TOOL_RESULT name="get_weather">>>', tool_result["content"])

    def test_mode_hybrid_reminder_references_agent_tools_marker(self):
        """The recency reminder points the model back at the AGENT_TOOLS
        section by literal name."""
        result = self._translate_with_mode("hybrid", pass_tools=True)
        last_user = result[-1]["content"]
        self.assertIn("<<<BEGIN_AGENT_TOOLS>>>", last_user)

    def test_mode_hybrid_tools_block_contains_disambiguation_sentence(self):
        """The AGENT_TOOLS block tells the model these are the only tools and
        to ignore competing definitions from elsewhere in the prompt."""
        result = self._translate_with_mode("hybrid", pass_tools=True)
        sys_content = result[0]["content"]
        self.assertIn("only tools available for use in this conversation", sys_content)

    def test_user_front_does_not_use_agent_instructions_marker(self):
        """AGENT_INSTRUCTIONS is hybrid-only; user_front never emits it."""
        result = self._translate_with_mode("user_front", pass_tools=True)
        joined = "\n".join(m["content"] for m in result)
        self.assertNotIn("AGENT_INSTRUCTIONS", joined)

    def test_user_front_does_not_use_agent_tools_marker(self):
        """AGENT_TOOLS is hybrid-only; user_front never emits it."""
        result = self._translate_with_mode("user_front", pass_tools=True)
        joined = "\n".join(m["content"] for m in result)
        self.assertNotIn("AGENT_TOOLS", joined)

    def test_user_front_does_not_use_user_message_marker(self):
        """USER_MESSAGE is hybrid-only; user_front uses USER_REQUEST on the
        latest user turn, as before."""
        result = self._translate_with_mode("user_front", pass_tools=True)
        joined = "\n".join(m["content"] for m in result)
        self.assertNotIn("USER_MESSAGE", joined)
        self.assertIn("<<<BEGIN_USER_REQUEST>>>", joined)

    def test_passthrough_does_not_use_any_new_markers(self):
        """passthrough is a verbatim bypass — none of the new hybrid markers
        appear."""
        with patch.object(proxy, "_PROMPT_MODE", "passthrough"):
            result = proxy.translate_history_and_apply_prompt(
                self.user_msgs, proxy.format_tools_to_text(self.tools), tools=self.tools,
            )
        joined = "\n".join(m["content"] for m in result)
        self.assertNotIn("AGENT_INSTRUCTIONS", joined)
        self.assertNotIn("AGENT_TOOLS", joined)
        self.assertNotIn("USER_MESSAGE", joined)

    def test_mode_user_front_intro_line_before_request(self):
        """user_front opens with the persona-preserve intro line, THEN the
        request, THEN the tool list (intro → delimited block → tools)."""
        result = self._translate_with_mode("user_front")
        last_user = result[-1]["content"]
        intro_pos = last_user.index("do not adopt a new identity")
        request_pos = last_user.index("<<<BEGIN_USER_REQUEST>>>")
        tools_pos = last_user.index("Available Tools")
        self.assertLess(
            intro_pos, request_pos,
            "user_front: persona intro line must come before the request",
        )
        self.assertLess(request_pos, tools_pos)


class TestHybridDetailTools(unittest.TestCase):
    """Hybrid mode inlines the FULL (pared) description of a project-managed
    set of "detail tools" (default `task,skill`) UNDER THE TOOL'S OWN RECENCY
    ENTRY. These are the tools whose valid argument values are a closed set
    opencode documents only as description prose (a `task`'s agent types, a
    `skill`'s skill names); the signature carries the parameter keys but not
    those values, so the description has to reach recency.

    Earlier versions rendered these in separate
    `<<<BEGIN_TOOL_DETAIL name="…">>>` blocks below the bullets; the
    consolidated format inlines them under the tool's entry so every fact
    about a tool (signature, guidance, valid argument values) sits together.
    """

    def setUp(self):
        p = patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", False)
        p.start()
        self.addCleanup(p.stop)
        # A task tool whose valid subagent_type values live ONLY in the
        # description prose (no JSON-Schema enum) — the exact opencode shape:
        # static boilerplate first, then the dynamic agent list introduced by
        # the header the proxy anchors on. The boilerplate is what paring drops.
        self.task_tool = {"function": {
            "name": "task",
            "description": (
                "Launch a new agent to handle complex, multistep tasks "
                "autonomously.\n\n"
                "When NOT to use the Task tool:\n"
                "- If you want to read a specific file path, use Read instead\n\n"
                "Available agent types and the tools they have access to:\n"
                "- general-purpose: research and multi-step tasks\n"
                "- Explore: fast read-only code search\n"
                "- Plan: software architect for implementation plans"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "prompt": {"type": "string"},
                    "subagent_type": {"type": "string"},
                },
                "required": ["description", "prompt", "subagent_type"],
            },
        }}
        self.bash_tool = {"function": {
            "name": "bash",
            "description": "Run a shell command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        }}
        self.user_msgs = [
            {"role": "system", "content": "You are opencode."},
            {"role": "user", "content": "do the thing"},
        ]

    def _translate(self, tools, flagged=("task", "skill")):
        tools_text = proxy.format_tools_to_text(tools)
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"), \
             patch.object(proxy, "_HYBRID_DETAIL_TOOLS", list(flagged)):
            return proxy.translate_history_and_apply_prompt(
                self.user_msgs, tools_text, tools=tools,
            )

    def test_detail_tools_is_project_managed_constant(self):
        # The detail-tools set is no longer read from an env var; it is a
        # project-managed constant tied to the opencode tools we ship for.
        self.assertEqual(proxy._HYBRID_DETAIL_TOOLS, ["task", "skill"])

    def test_extract_tool_details_returns_flagged_pairs_in_order(self):
        details = proxy._extract_tool_details(
            [self.bash_tool, self.task_tool], ["task", "bash"],
        )
        self.assertEqual([n for n, _ in details], ["task", "bash"])
        self.assertIn("Available agent types", dict(details)["task"])

    def test_extract_tool_details_skips_absent_and_empty(self):
        empty_desc = {"function": {"name": "skill", "description": "  ",
                                   "parameters": {}}}
        details = proxy._extract_tool_details(
            [self.task_tool, empty_desc], ["skill", "task", "missing"],
        )
        # skill present but empty desc → skipped; missing absent → skipped.
        self.assertEqual([n for n, _ in details], ["task"])

    def test_extract_tool_details_no_tools_returns_empty(self):
        self.assertEqual(proxy._extract_tool_details(None, ["task"]), [])

    def test_detail_inlined_under_flagged_task_tool_entry(self):
        last_user = self._translate([self.task_tool])[-1]["content"]
        # The closed-set values (agent types) reach recency verbatim.
        self.assertIn("Available agent types", last_user)
        self.assertIn("general-purpose", last_user)
        self.assertIn("Explore", last_user)
        # …but the static boilerplate is pared out (redundant with the
        # stable-prefix copy and is what dilutes recency).
        self.assertNotIn("Launch a new agent", last_user)
        self.assertNotIn("When NOT to use", last_user)
        # No separate TOOL_DETAIL block in the consolidated format.
        self.assertNotIn("<<<BEGIN_TOOL_DETAIL", last_user)
        # The inlined description sits immediately after the task entry's
        # signature/guidance line — between `task(...)` and the next bullet.
        task_entry_start = last_user.index(
            "- task(description, prompt, subagent_type)"
        )
        next_bullet = last_user.find("\n- ", task_entry_start + 1)
        if next_bullet == -1:
            # task is the last bullet; everything after it through the closing
            # ']' is the inlined description.
            next_bullet = last_user.index("]", task_entry_start)
        task_block = last_user[task_entry_start:next_bullet]
        self.assertIn("Available agent types", task_block)

    def test_detail_inlined_block_sits_inside_reminder_after_request(self):
        last_user = self._translate([self.task_tool])[-1]["content"]
        request_end = last_user.index("<<<END_USER_REQUEST>>>")
        reminder_pos = last_user.index("Reminder")
        agents_pos = last_user.index("Available agent types")
        # USER_REQUEST first, then reminder, then the inlined description.
        self.assertLess(request_end, reminder_pos)
        self.assertLess(reminder_pos, agents_pos)
        self.assertIn("do the thing", last_user)

    def test_no_inlined_detail_for_unflagged_tool(self):
        # bash isn't in the flagged set → its entry has the guidance line
        # but no inlined description content.
        last_user = self._translate([self.bash_tool])[-1]["content"]
        self.assertNotIn("<<<BEGIN_TOOL_DETAIL", last_user)
        # bash's own description ("Run a shell command") should not appear
        # verbatim from the tools array (it's not a detail tool); only the
        # _HYBRID_TOOL_GUIDANCE one-liner accompanies the signature.
        self.assertIn("- bash(command)", last_user)

    def test_inlined_detail_only_for_present_flagged_tools(self):
        # skill is flagged but absent from the toolset; only task gets its
        # description inlined. bash is present but unflagged, so its tools-
        # array description ("Run a shell command") is NOT inlined.
        last_user = self._translate([self.task_tool, self.bash_tool])[-1]["content"]
        self.assertIn("Available agent types", last_user)
        # No verbatim copy of bash's description ("Run a shell command") —
        # only the guidance one-liner ("Run a shell command (terminal:
        # git, npm, docker, etc.)") from _HYBRID_TOOL_GUIDANCE.
        self.assertNotIn("<<<BEGIN_TOOL_DETAIL", last_user)

    def test_empty_flagged_set_disables_inlined_detail(self):
        last_user = self._translate([self.task_tool], flagged=[])[-1]["content"]
        self.assertNotIn("<<<BEGIN_TOOL_DETAIL", last_user)
        # The task closed-set values don't reach recency at all.
        self.assertNotIn("Available agent types", last_user)
        # The rest of the reminder is unaffected.
        self.assertIn("Reminder", last_user)
        self.assertIn("task(description, prompt, subagent_type)", last_user)

    def test_inlined_detail_also_on_tool_result_turn(self):
        """The inlined description must reach recency on tool-result turns
        too — the model still needs the valid agent types when continuing
        the loop."""
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "task", "arguments": {}}}]},
            {"role": "tool", "tool_name": "task", "content": "subagent done"},
        ]
        tools_text = proxy.format_tools_to_text([self.task_tool])
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"), \
             patch.object(proxy, "_HYBRID_DETAIL_TOOLS", ["task", "skill"]):
            out = proxy.translate_history_and_apply_prompt(
                msgs, tools_text, tools=[self.task_tool],
            )
        c = out[-1]["content"]
        self.assertIn("Available agent types", c)
        self.assertNotIn("<<<BEGIN_TOOL_DETAIL", c)

    def test_user_front_never_emits_inlined_detail(self):
        """The inlined description / consolidated recency entries are
        hybrid-only — user_front has its own (different) scaffold."""
        tools_text = proxy.format_tools_to_text([self.task_tool])
        with patch.object(proxy, "_PROMPT_MODE", "user_front"), \
             patch.object(proxy, "_HYBRID_DETAIL_TOOLS", ["task", "skill"]):
            out = proxy.translate_history_and_apply_prompt(
                self.user_msgs, tools_text, tools=[self.task_tool],
            )
        joined = "\n".join(m["content"] for m in out)
        # No leftover TOOL_DETAIL framing from the prior format.
        self.assertNotIn("TOOL_DETAIL", joined)
        # No "Tools — one entry per tool" recency legend from the hybrid
        # consolidated format.
        self.assertNotIn("Tools — one entry per tool", joined)


class TestHybridConsolidatedRecency(unittest.TestCase):
    """The consolidated recency format puts everything for a given tool in
    one place: signature, one-line guidance from `_HYBRID_TOOL_GUIDANCE`, and
    (for detail tools) the verbatim closed-set argument values. The five
    earlier labelled bullets (Agency/Tools/Workflow/Honesty/Environment)
    collapse to three (Operating/Honesty/Environment), and the live user
    request moves to the FRONT of the recency block (USER_REQUEST delimiter)
    so the model's most-recent attention lands on the ask, not the rules.
    """

    def setUp(self):
        p = patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", False)
        p.start()
        self.addCleanup(p.stop)
        # A representative shipped opencode toolset — every tool is in
        # `_HYBRID_TOOL_GUIDANCE`, and `task`/`skill` are detail tools.
        self.tools = [
            {"function": {
                "name": "bash",
                "description": "Run a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "description": {"type": "string"},
                        "timeout": {"type": "integer"},
                        "workdir": {"type": "string"},
                    },
                    "required": ["command", "description"],
                },
            }},
            {"function": {
                "name": "todowrite",
                "description": "Maintain a todo list",
                "parameters": {
                    "type": "object",
                    "properties": {"todos": {"type": "array"}},
                    "required": ["todos"],
                },
            }},
            {"function": {
                "name": "task",
                "description": (
                    "Launch a sub-agent.\n\n"
                    "Available agent types and the tools they have access to:\n"
                    "- explore: codebase search\n"
                    "- general: multi-step work\n"
                    "- yolo: auto-approve permissions"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "prompt": {"type": "string"},
                        "subagent_type": {"type": "string"},
                    },
                    "required": ["description", "prompt", "subagent_type"],
                },
            }},
            {"function": {
                "name": "skill",
                "description": (
                    "Load a specialized skill.\n"
                    "## Available Skills\n"
                    "- save: file a conversation\n"
                    "- wiki: scaffold a vault"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            }},
        ]
        self.user_msgs = [
            {"role": "system", "content": "You are opencode."},
            {"role": "user", "content": "do the thing"},
        ]

    def _translate(self):
        tools_text = proxy.format_tools_to_text(self.tools)
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            return proxy.translate_history_and_apply_prompt(
                self.user_msgs, tools_text, tools=self.tools,
            )

    def test_user_request_wrapped_and_at_front(self):
        """The live user request is wrapped in USER_REQUEST and placed
        BEFORE the reminder — the order swap the user asked for."""
        c = self._translate()[-1]["content"]
        self.assertIn("<<<BEGIN_USER_REQUEST>>>\ndo the thing\n<<<END_USER_REQUEST>>>", c)
        self.assertLess(c.index("<<<BEGIN_USER_REQUEST>>>"), c.index("Reminder"))

    def test_no_user_message_wrap_on_live_turn(self):
        """USER_MESSAGE is for prior user turns only. The live ask gets
        USER_REQUEST instead — they're mutually exclusive on the live turn."""
        c = self._translate()[-1]["content"]
        self.assertNotIn("<<<BEGIN_USER_MESSAGE>>>", c)
        self.assertNotIn("<<<END_USER_MESSAGE>>>", c)

    def test_three_bullets_only_no_agency_tools_workflow_labels(self):
        """Agency/Tools/Workflow are merged into one Operating bullet, so the
        old separate labels are gone. Honesty/Environment keep their own
        bullets."""
        c = self._translate()[-1]["content"]
        self.assertIn("- Operating:", c)
        self.assertIn("- Honesty:", c)
        self.assertIn("- Environment:", c)
        self.assertNotIn("- Agency:", c)
        self.assertNotIn("- Tools:", c)
        self.assertNotIn("- Workflow:", c)

    def test_operating_carries_merged_agency_tools_workflow_content(self):
        """The Operating bullet must preserve the load-bearing content from
        each of the three merged bullets — losing any of these is a real
        regression."""
        c = self._translate()[-1]["content"]
        operating = c[c.index("- Operating:"):c.index("- Honesty:")]
        # Agency content: tools really execute, results are real,
        # don't downgrade to listing commands, opencode named.
        self.assertIn("really execute", operating)
        self.assertIn("results you get back are real", operating)
        self.assertIn("don't downgrade to listing commands", operating)
        self.assertIn("opencode", operating)
        # Tools content: JSON envelope + no-fabricated-results.
        self.assertIn('"name": ...', operating)
        self.assertIn('"arguments": ...', operating)
        self.assertIn("do not invent", operating)
        # Workflow content: prefer listed tool, todowrite, task agents,
        # parallel/concurrent.
        self.assertIn("prefer a listed tool", operating)
        self.assertIn("todowrite", operating)
        self.assertIn("Launch `task` agents", operating)
        self.assertIn("concurrently", operating)
        # Fallback ("ask or answer") + AGENT_TOOLS pointer survive.
        self.assertIn("just ask or answer", operating)
        self.assertIn("<<<BEGIN_AGENT_TOOLS>>>", operating)

    def test_per_tool_entry_format_with_guidance(self):
        """Tools in `_HYBRID_TOOL_GUIDANCE` get one entry: signature + ` — `
        + guidance one-liner. All info for that tool sits in one place."""
        c = self._translate()[-1]["content"]
        # bash gets its full signature plus the guidance one-liner.
        self.assertIn(
            "- bash(command, description, [timeout], [workdir]) — Run a shell command",
            c,
        )
        # todowrite's guidance is the headline misuse fix.
        self.assertIn(
            "- todowrite(todos) — Maintain a structured todo list",
            c,
        )
        self.assertIn("Exactly ONE item may be `in_progress`", c)

    def test_signature_format_legend_present(self):
        """The legend above the per-tool entries teaches the model what the
        `name(required, [optional])` shape means."""
        c = self._translate()[-1]["content"]
        self.assertIn("Signature format: name(required, [optional])", c)
        self.assertIn("parameter names must match exactly", c)
        # The legend names the exact misuse that surfaces in the wild.
        self.assertIn("`filename` fails where `filePath` is required", c)

    def test_skill_detail_inlined_under_skill_entry(self):
        """The skill description content (the closed set of valid `name`
        values) is inlined directly under the skill tool's own entry — same
        information as the old TOOL_DETAIL block, but in one place with the
        rest of the skill tool's info."""
        c = self._translate()[-1]["content"]
        skill_entry_start = c.index("- skill(name)")
        next_bullet_after_skill = c.find("\n- ", skill_entry_start + 1)
        if next_bullet_after_skill == -1:
            next_bullet_after_skill = c.index("]", skill_entry_start)
        skill_block = c[skill_entry_start:next_bullet_after_skill]
        self.assertIn("Available Skills", skill_block)
        self.assertIn("save", skill_block)
        self.assertIn("wiki", skill_block)

    def test_task_detail_inlined_under_task_entry_pared(self):
        """The task description's agent-list section is inlined under the
        task entry; the static boilerplate is pared out (redundant with the
        stable prefix)."""
        c = self._translate()[-1]["content"]
        task_entry_start = c.index("- task(description, prompt, subagent_type)")
        next_bullet = c.find("\n- ", task_entry_start + 1)
        if next_bullet == -1:
            next_bullet = c.index("]", task_entry_start)
        task_block = c[task_entry_start:next_bullet]
        self.assertIn("Available agent types", task_block)
        self.assertIn("explore", task_block)
        self.assertIn("general", task_block)
        self.assertIn("yolo", task_block)
        # Paring keeps only the agent-list section of the tool's verbatim
        # description; the inlined description starts with the header. Use
        # the indented-line prefix ("  ") to isolate the inlined description
        # from the guidance one-liner (which sits on the same line as the
        # signature and may share phrases with the tool's prose).
        indented_lines = [
            ln for ln in task_block.splitlines() if ln.startswith("  ")
        ]
        inlined = "\n".join(indented_lines)
        self.assertTrue(inlined.lstrip().startswith(
            proxy._OPENCODE_TASK_AGENTS_HEADER
        ))

    def test_unknown_tool_renders_bare_signature(self):
        """A tool absent from `_HYBRID_TOOL_GUIDANCE` (e.g. a custom MCP
        tool the user added) renders as just `- name(signature)` with no
        guidance text. Format degrades gracefully."""
        custom = {"function": {
            "name": "mcp_thing",
            "description": "custom",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        }}
        tools = [custom]
        tools_text = proxy.format_tools_to_text(tools)
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            out = proxy.translate_history_and_apply_prompt(
                self.user_msgs, tools_text, tools=tools,
            )
        c = out[-1]["content"]
        # Bare signature line — no ` — ` separator and no guidance text.
        # mcp_thing is the only tool, so it's also the last entry and the
        # reminder's closing `]` lands on the same line — strip it before
        # asserting on the bare entry shape.
        self.assertIn("- mcp_thing(q)", c)
        line = next(ln for ln in c.splitlines() if ln.startswith("- mcp_thing"))
        self.assertEqual(line.rstrip("]"), "- mcp_thing(q)")
        # The em-dash separator only appears when guidance is present.
        self.assertNotIn("- mcp_thing(q) —", c)

    def test_no_tool_detail_blocks_anywhere(self):
        """The legacy `<<<BEGIN_TOOL_DETAIL>>>` framing is gone in the
        consolidated format — its content is inlined per tool instead."""
        c = self._translate()[-1]["content"]
        self.assertNotIn("<<<BEGIN_TOOL_DETAIL", c)
        self.assertNotIn("<<<END_TOOL_DETAIL>>>", c)
        self.assertNotIn(
            "Valid argument values for the tools below", c,
        )

    def test_reminder_appears_once_at_end_of_message(self):
        """Single reminder block per turn. The user request comes first;
        the reminder follows. Asserts the simple ordering invariant."""
        c = self._translate()[-1]["content"]
        self.assertEqual(c.count("[Reminder — operating rules for this turn."), 1)
        request_pos = c.index("<<<BEGIN_USER_REQUEST>>>")
        reminder_pos = c.index("[Reminder")
        self.assertLess(request_pos, reminder_pos)

    def test_tool_result_turn_skips_user_request_wrap(self):
        """On a tool-result turn the content already has TOOL_RESULT markers
        delimiting it; the builder must NOT also wrap in USER_REQUEST. The
        TOOL_RESULT block stays at the front, reminder behind it."""
        msgs = [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "bash", "arguments": {}}}]},
            {"role": "tool", "tool_name": "bash", "content": "72F sunny"},
        ]
        tools_text = proxy.format_tools_to_text(self.tools)
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            out = proxy.translate_history_and_apply_prompt(
                msgs, tools_text, tools=self.tools,
            )
        c = out[-1]["content"]
        self.assertIn("<<<BEGIN_TOOL_RESULT", c)
        self.assertNotIn("<<<BEGIN_USER_REQUEST>>>", c)
        # TOOL_RESULT content at the front, reminder behind it.
        self.assertLess(c.index("<<<END_TOOL_RESULT>>>"), c.index("Reminder"))


class TestHostOsSetup(unittest.TestCase):
    """`_setup_host_os` reads HARNESS_HOST_OS (injected by the harness CLI from
    harness_detect_os) into `_HOST_OS`. Only linux/macos/windows are honoured;
    anything else — unset, empty, or "unknown" — normalises to "" so the
    Environment line drops the host-OS parenthetical."""

    def tearDown(self):
        # Restore the module default so other tests see a stable global.
        proxy._HOST_OS = ""

    def test_setup_honours_recognised_values(self):
        for val in ("linux", "macos", "windows"):
            with patch.dict(os.environ, {"HARNESS_HOST_OS": val}):
                proxy._setup_host_os()
                self.assertEqual(proxy._HOST_OS, val)

    def test_setup_normalises_case_and_whitespace(self):
        with patch.dict(os.environ, {"HARNESS_HOST_OS": "  Windows "}):
            proxy._setup_host_os()
            self.assertEqual(proxy._HOST_OS, "windows")

    def test_setup_unknown_becomes_empty(self):
        with patch.dict(os.environ, {"HARNESS_HOST_OS": "unknown"}):
            proxy._setup_host_os()
            self.assertEqual(proxy._HOST_OS, "")

    def test_setup_unset_becomes_empty(self):
        env_no_var = {k: v for k, v in os.environ.items()
                      if k != "HARNESS_HOST_OS"}
        with patch.dict(os.environ, env_no_var, clear=True):
            proxy._setup_host_os()
            self.assertEqual(proxy._HOST_OS, "")


class TestTaskDescriptionParing(unittest.TestCase):
    """`task`'s inlined recency description is pared to its agent-list section;
    the static boilerplate (redundant at recency) is dropped. The parse anchors
    on a header string opencode emits in ToolRegistry.describeTask. `skill` is
    left whole.

    REAL_TASK_DESCRIPTION below is a faithful copy of what opencode hands the
    proxy: static boilerplate, then the dynamic agent list. The opening lines
    are verbatim from opencode's bundled task prompt (var `an`) and the list
    rows follow describeTask's `- <name>: <description>` shape.

    CANARY: this fixture is the cross-version guard the issue asked for. On every
    opencode bump, re-extract the real description and refresh this fixture, e.g.

        strings -n 6 node_modules/opencode-linux-x64/bin/opencode \\
          | grep -m1 'Available agent types and the tools they have access to'

    If opencode renames/reflows the header, refreshing the fixture makes
    `test_anchor_is_the_string_opencode_emits` /
    `test_paring_keeps_agent_list_drops_boilerplate` fail loudly — the signal to
    update `proxy._OPENCODE_TASK_AGENTS_HEADER`. Until then, paring degrades
    safely: an unrecognised description is echoed whole, never silently dropped.
    """

    REAL_TASK_DESCRIPTION = (
        "Launch a new agent to handle complex, multistep tasks autonomously.\n\n"
        "When using the Task tool, you must specify a subagent_type parameter "
        "to select which agent type to use.\n\n"
        "When NOT to use the Task tool:\n"
        "- If you want to read a specific file path, use the Read or Glob tool "
        "instead of the Task tool, to find the match more quickly\n"
        "- If no available agent is a good fit for the task, use other tools "
        "directly\n\n\n"
        "Usage notes:\n"
        "1. Launch multiple agents concurrently whenever possible, to maximize "
        "performance; to do that, use a single message with multiple tool uses\n"
        "2. When the agent is done, it will return a single message back to "
        "you.\n\n"
        "Available agent types and the tools they have access to:\n"
        "- Explore: fast read-only code search\n"
        "- general-purpose: research and multi-step tasks\n"
        "- Plan: software architect for implementation plans"
    )

    def test_anchor_is_the_string_opencode_emits(self):
        # The header constant must appear verbatim in the real description.
        self.assertIn(
            proxy._OPENCODE_TASK_AGENTS_HEADER, self.REAL_TASK_DESCRIPTION
        )

    def test_paring_keeps_agent_list_drops_boilerplate(self):
        pared = proxy._pare_task_description(self.REAL_TASK_DESCRIPTION)
        # Starts exactly at the header — no boilerplate ahead of it.
        self.assertTrue(pared.startswith(proxy._OPENCODE_TASK_AGENTS_HEADER))
        # The closed-set agent values survive intact, in order.
        self.assertIn("- Explore: fast read-only code search", pared)
        self.assertIn("- general-purpose: research and multi-step tasks", pared)
        self.assertIn("- Plan: software architect for implementation plans", pared)
        # The boilerplate is gone.
        self.assertNotIn("Launch a new agent", pared)
        self.assertNotIn("When NOT to use", pared)
        self.assertNotIn("Usage notes", pared)

    def test_paring_is_idempotent(self):
        once = proxy._pare_task_description(self.REAL_TASK_DESCRIPTION)
        twice = proxy._pare_task_description(once)
        self.assertEqual(once, twice)

    def test_paring_falls_back_to_full_when_header_absent(self):
        # A future opencode that reflows the header: keep the whole description
        # rather than drop the agent list. Degrade to more tokens, never to a
        # silent loss of the closed set.
        drifted = "Spawn a worker.\nValid workers:\n- alpha\n- beta"
        self.assertEqual(proxy._pare_task_description(drifted), drifted)

    def test_only_task_is_pared_skill_left_whole(self):
        # Even a (contrived) skill whose description contains the task header is
        # NOT pared — the transform is keyed on the tool name `task` only.
        skill_tool = {"function": {
            "name": "skill",
            "description": (
                "Execute a skill.\n"
                "Available agent types and the tools they have access to:\n"
                "- noise"
            ),
            "parameters": {"type": "object", "properties": {}},
        }}
        task_tool = {"function": {
            "name": "task",
            "description": self.REAL_TASK_DESCRIPTION,
            "parameters": {"type": "object", "properties": {}},
        }}
        details = dict(proxy._extract_tool_details(
            [skill_tool, task_tool], ["skill", "task"],
        ))
        self.assertTrue(details["skill"].startswith("Execute a skill."))
        self.assertTrue(
            details["task"].startswith(proxy._OPENCODE_TASK_AGENTS_HEADER)
        )


class TestPassthroughMode(unittest.TestCase):
    """`passthrough` mode is the benchmark control: skip every harness-side
    mediation (cooperative-prompt injection, system→user rewrite, history
    translation) and forward the agent's request verbatim. The matching
    tools-passthrough on the catch_all side is covered indirectly: this
    class verifies translate_history_and_apply_prompt's contract.
    """

    def test_validator_accepts_passthrough(self):
        """`passthrough` is in the valid PROMPT_MODE set; it must not coerce
        to the default (hybrid) — a value the proxy sees here arrived via the
        `--prompt-mode` flag, so coercing a valid mode would break benchmarks."""
        with patch.dict(os.environ, {"PROXY_PROMPT_MODE": "passthrough"}):
            proxy._setup_prompt_mode()
            self.assertEqual(proxy._PROMPT_MODE, "passthrough")

    def test_passthrough_skips_cooperative_prompt_injection(self):
        """With tools provided, passthrough does NOT wrap the last user
        message in cooperative-prompt scaffolding. Other modes always do."""
        msgs = [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "weather?"},
        ]
        tools_text = "Tool Name: `get_weather`"
        with patch.object(proxy, "_PROMPT_MODE", "passthrough"):
            out = proxy.translate_history_and_apply_prompt(msgs, tools_text)
        self.assertEqual(out, msgs)
        self.assertNotIn("<<<BEGIN_USER_REQUEST>>>", out[-1]["content"])
        self.assertNotIn("Available Tools", out[-1]["content"])

    def test_passthrough_skips_system_to_user_rewrite(self):
        """passthrough leaves system role as system even when
        _CHANGE_SYSTEM_TO_USER is true (the default). Other modes rewrite."""
        msgs = [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "hi"},
        ]
        with patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True), \
             patch.object(proxy, "_PROMPT_MODE", "passthrough"):
            out = proxy.translate_history_and_apply_prompt(msgs, "")
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[0]["content"], "You are a coding agent.")

    def test_passthrough_does_not_translate_assistant_tool_calls(self):
        """Other modes render assistant tool_calls into a markdown JSON
        block in content. passthrough leaves the message structure alone."""
        msgs = [
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [{
                    "function": {"name": "get_weather", "arguments": {"city": "Atlanta"}}
                }],
            },
        ]
        with patch.object(proxy, "_PROMPT_MODE", "passthrough"):
            out = proxy.translate_history_and_apply_prompt(msgs, "")
        self.assertEqual(out[1]["content"], "checking")
        self.assertIn("tool_calls", out[1])
        self.assertNotIn("```json", out[1]["content"])

    def test_passthrough_does_not_wrap_tool_results(self):
        """Other modes wrap role:tool messages in
        <<<BEGIN_TOOL_RESULT>>>/<<<END_TOOL_RESULT>>> markers and fold them
        into a user message. passthrough keeps them as role:tool."""
        msgs = [
            {"role": "user", "content": "weather?"},
            {"role": "tool", "tool_name": "get_weather", "content": "72F sunny"},
        ]
        with patch.object(proxy, "_PROMPT_MODE", "passthrough"):
            out = proxy.translate_history_and_apply_prompt(msgs, "")
        self.assertEqual(out[1]["role"], "tool")
        self.assertEqual(out[1]["content"], "72F sunny")
        self.assertNotIn("<<<BEGIN_TOOL_RESULT", str(out))

    def test_passthrough_returns_shallow_copy(self):
        """The return value is a fresh list of fresh dicts so a caller
        mutating the result can't corrupt the original_messages structure
        that may still be in use upstream (e.g. for token accounting)."""
        msgs = [{"role": "user", "content": "hi"}]
        with patch.object(proxy, "_PROMPT_MODE", "passthrough"):
            out = proxy.translate_history_and_apply_prompt(msgs, "")
        self.assertIsNot(out, msgs)
        self.assertIsNot(out[0], msgs[0])
        # Equal content though
        self.assertEqual(out, msgs)

    def test_passthrough_empty_returns_empty(self):
        with patch.object(proxy, "_PROMPT_MODE", "passthrough"):
            self.assertEqual(proxy.translate_history_and_apply_prompt([], ""), [])


class TestToolResultDelimiting(unittest.TestCase):
    """Tool results are wrapped in <<<BEGIN_TOOL_RESULT>>> / <<<END_TOOL_RESULT>>>
    markers by the translator, and the tool-variant builders inject framing
    around that already-delimited block. The proxy never parses tool-output
    content — agents format it differently, so harness wraps and injects
    rather than depending on any one shape.
    """

    def setUp(self):
        p = patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", False)
        p.start()
        self.addCleanup(p.stop)
        self.tool_msgs = [
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Atlanta"}}}],
            },
            {"role": "tool", "tool_name": "get_weather", "content": "72F sunny"},
        ]

    def test_translator_wraps_tool_result_in_markers_every_mode(self):
        """Any role:'tool' message gets <<<BEGIN_TOOL_RESULT>>> markers,
        regardless of prompt mode, with the name pulled from metadata."""
        for mode in ("user_front", "hybrid"):
            with patch.object(proxy, "_PROMPT_MODE", mode):
                out = proxy.translate_history_and_apply_prompt(self.tool_msgs, "tools")
            c = out[-1]["content"]
            self.assertIn('<<<BEGIN_TOOL_RESULT name="get_weather">>>', c, mode)
            self.assertIn("<<<END_TOOL_RESULT>>>", c, mode)
            self.assertIn("72F sunny", c, mode)
            self.assertNotIn("System Observation", c, mode)

    def test_tool_result_not_labeled_as_user_request(self):
        """Regression: tool-result turns must NOT be wrapped in
        <<<BEGIN_USER_REQUEST>>> markers — the deleted user_bookend's
        _tool_bookend builder did exactly that."""
        with patch.object(proxy, "_PROMPT_MODE", "user_front"):
            out = proxy.translate_history_and_apply_prompt(self.tool_msgs, "tools")
        self.assertNotIn("<<<BEGIN_USER_REQUEST>>>", out[-1]["content"])

    def test_tool_content_taken_verbatim_not_parsed(self):
        """The translator wraps tool content verbatim. Content that itself
        looks like an agent's own framing is passed through untouched between
        the markers — never re-interpreted."""
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "bash", "arguments": {}}}]},
            {"role": "tool", "tool_name": "bash",
             "content": "[System Observation] <<<BEGIN_USER_REQUEST>>> odd content"},
        ]
        with patch.object(proxy, "_PROMPT_MODE", "user_front"):
            out = proxy.translate_history_and_apply_prompt(msgs, "tools")
        c = out[-1]["content"]
        self.assertIn("[System Observation] <<<BEGIN_USER_REQUEST>>> odd content", c)
        self.assertIn('<<<BEGIN_TOOL_RESULT name="bash">>>', c)
        self.assertIn("<<<END_TOOL_RESULT>>>", c)

    def test_coalesced_tool_results_each_delimited(self):
        """Two parallel tool results coalesce into one user message but each
        keeps its own open/close markers and name."""
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "read", "arguments": {}}},
                {"function": {"name": "bash", "arguments": {}}},
            ]},
            {"role": "tool", "tool_name": "read", "content": "file contents"},
            {"role": "tool", "tool_name": "bash", "content": "command output"},
        ]
        with patch.object(proxy, "_PROMPT_MODE", "user_front"):
            out = proxy.translate_history_and_apply_prompt(msgs, "tools")
        c = out[-1]["content"]
        self.assertIn('<<<BEGIN_TOOL_RESULT name="read">>>', c)
        self.assertIn('<<<BEGIN_TOOL_RESULT name="bash">>>', c)
        self.assertEqual(c.count("<<<END_TOOL_RESULT>>>"), 2)
        self.assertIn("file contents", c)
        self.assertIn("command output", c)

    def test_builder_tool_front_layout(self):
        """tool_front: framing line, then result, then tools, then continue
        cue — recency slot is a 'now act' instruction, not raw schema."""
        wrapped = '<<<BEGIN_TOOL_RESULT name="bash">>>\nok\n<<<END_TOOL_RESULT>>>'
        out = proxy.build_cooperative_prompt_tool_front(wrapped, "TOOLS_HERE")
        self.assertIn("NOT a message from the user", out)
        self.assertIn(wrapped, out)
        self.assertIn("TOOLS_HERE", out)
        self.assertIn("End of tool definitions", out)
        framing_pos = out.index("NOT a message from the user")
        result_pos = out.index(wrapped)
        tools_pos = out.index("TOOLS_HERE")
        cue_pos = out.index("End of tool definitions")
        self.assertLess(framing_pos, result_pos)
        self.assertLess(result_pos, tools_pos)
        self.assertLess(tools_pos, cue_pos)
        self.assertNotIn("<<<BEGIN_USER_REQUEST>>>", out)

    def test_tool_result_name_resolved_via_tool_call_id(self):
        """A role:'tool' message with no name field but a tool_call_id is
        labeled by correlating that id against the originating assistant
        tool_calls — the shape some agents send."""
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "toolu_abc", "function": {"name": "Bash", "arguments": {}}},
            ]},
            {"role": "tool", "tool_call_id": "toolu_abc", "content": "ok"},
        ]
        with patch.object(proxy, "_PROMPT_MODE", "user_front"):
            out = proxy.translate_history_and_apply_prompt(msgs, "tools")
        c = out[-1]["content"]
        self.assertIn('<<<BEGIN_TOOL_RESULT name="Bash">>>', c)
        self.assertNotIn("unknown_tool", c)

    def test_tool_result_name_falls_back_to_positional_order(self):
        """With neither a name field nor a tool_call_id, the name is
        recovered from the order of the assistant's tool_calls."""
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "read", "arguments": {}}},
                {"function": {"name": "write", "arguments": {}}},
            ]},
            {"role": "tool", "content": "first result"},
            {"role": "tool", "content": "second result"},
        ]
        with patch.object(proxy, "_PROMPT_MODE", "user_front"):
            out = proxy.translate_history_and_apply_prompt(msgs, "tools")
        c = out[-1]["content"]
        self.assertIn('<<<BEGIN_TOOL_RESULT name="read">>>', c)
        self.assertIn('<<<BEGIN_TOOL_RESULT name="write">>>', c)
        self.assertNotIn("unknown_tool", c)

    def test_tool_result_name_prefers_explicit_field(self):
        """An explicit tool_name field wins over id correlation and
        positional order — opencode and ollama send it directly."""
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "toolu_x", "function": {"name": "WrongName", "arguments": {}}},
            ]},
            {"role": "tool", "tool_name": "RightName", "tool_call_id": "toolu_x",
             "content": "ok"},
        ]
        with patch.object(proxy, "_PROMPT_MODE", "user_front"):
            out = proxy.translate_history_and_apply_prompt(msgs, "tools")
        c = out[-1]["content"]
        self.assertIn('<<<BEGIN_TOOL_RESULT name="RightName">>>', c)
        self.assertNotIn("WrongName", c)

    def test_tool_result_name_unknown_when_no_metadata(self):
        """No name field, no id, and no preceding tool_calls to draw from →
        labeled unknown_tool rather than crashing."""
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "tool", "content": "orphan result"},
        ]
        with patch.object(proxy, "_PROMPT_MODE", "user_front"):
            out = proxy.translate_history_and_apply_prompt(msgs, "tools")
        c = out[-1]["content"]
        self.assertIn('<<<BEGIN_TOOL_RESULT name="unknown_tool">>>', c)
        self.assertIn("orphan result", c)


class TestChangeSystemToUser(unittest.TestCase):
    """Tests for the system→user conversion (the `_CHANGE_SYSTEM_TO_USER`
    constant, hardcoded True). The conversion runs AFTER prompt-mode injection
    and rewrites the head-of-conversation system message as a user message,
    with a stub assistant turn between it and the actual first user message.
    The constant (rather than an inlined `True`) keeps the non-conversion path
    testable; the env var that used to set it is gone."""

    def test_change_system_to_user_converts_system_to_user(self):
        """When `_CHANGE_SYSTEM_TO_USER` is True (always) and a system message
        is present, it gets converted to a user message with a stub
        assistant turn before the actual user message."""
        with patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True):
            with patch.object(proxy, "_PROMPT_MODE", "user_front"):
                messages = [
                    {"role": "system", "content": "You are a coding agent."},
                    {"role": "user", "content": "Hello"},
                ]
                result = proxy.translate_history_and_apply_prompt(messages, "")
                self.assertEqual(len(result), 3)
                self.assertEqual(result[0]["role"], "user")
                self.assertEqual(result[0]["content"], "You are a coding agent.")
                self.assertEqual(result[1]["role"], "assistant")
                self.assertEqual(result[1]["content"], "I understand the instructions above.")
                self.assertEqual(result[2]["role"], "user")
                self.assertIn("Hello", result[2]["content"])

    def test_change_system_to_user_disabled_keeps_system(self):
        """When `_CHANGE_SYSTEM_TO_USER` is False (the test-only non-conversion
        path), system messages pass through unchanged."""
        with patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", False):
            with patch.object(proxy, "_PROMPT_MODE", "user_front"):
                messages = [
                    {"role": "system", "content": "You are a coding agent."},
                    {"role": "user", "content": "Hello"},
                ]
                result = proxy.translate_history_and_apply_prompt(messages, "")
                self.assertEqual(len(result), 2)
                self.assertEqual(result[0]["role"], "system")
                self.assertEqual(result[0]["content"], "You are a coding agent.")

    def test_change_system_to_user_with_no_system_message(self):
        """When there's no system message, conversion is a no-op."""
        with patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True):
            with patch.object(proxy, "_PROMPT_MODE", "user_front"):
                messages = [
                    {"role": "user", "content": "Hello"},
                ]
                result = proxy.translate_history_and_apply_prompt(messages, "")
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["role"], "user")
                # No stub assistant inserted; just the user message
                for msg in result:
                    self.assertNotEqual(msg["role"], "assistant")

    def test_change_system_to_user_with_empty_system(self):
        """When the system message is empty/whitespace, it's dropped
        entirely rather than producing an empty user message."""
        with patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True):
            with patch.object(proxy, "_PROMPT_MODE", "user_front"):
                messages = [
                    {"role": "system", "content": "   \n  "},
                    {"role": "user", "content": "Hello"},
                ]
                result = proxy.translate_history_and_apply_prompt(messages, "")
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["role"], "user")

    def test_change_system_to_user_concatenates_multiple_systems(self):
        """Multiple system messages are coalesced (existing behavior)
        then converted as a single combined user message."""
        with patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True):
            with patch.object(proxy, "_PROMPT_MODE", "user_front"):
                messages = [
                    {"role": "system", "content": "You are a coding agent."},
                    {"role": "system", "content": "Project: foo bar."},
                    {"role": "user", "content": "Hello"},
                ]
                result = proxy.translate_history_and_apply_prompt(messages, "")
                # Combined user + stub assistant + actual user = 3 messages
                self.assertEqual(len(result), 3)
                self.assertEqual(result[0]["role"], "user")
                self.assertIn("You are a coding agent.", result[0]["content"])
                self.assertIn("Project: foo bar.", result[0]["content"])
                self.assertIn("\n\n", result[0]["content"])  # the separator
                self.assertEqual(result[1]["role"], "assistant")

    def test_change_system_to_user_with_hybrid_mode_injection(self):
        """When PROMPT_MODE='hybrid' AND CHANGE_SYSTEM_TO_USER is on, the
        tool definitions get injected into the system message FIRST (the
        stable prefix), then the loaded system message gets converted to a
        user message at index 0. The recency reminder lands on the actual
        last user message — index 2 (post-stub-assistant)."""
        with patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True):
            with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
                messages = [
                    {"role": "system", "content": "You are a coding agent."},
                    {"role": "user", "content": "Hello"},
                ]
                tools_text = "Tool Name: `Bash`\nRun shell command"
                result = proxy.translate_history_and_apply_prompt(messages, tools_text)
                self.assertEqual(result[0]["role"], "user")
                self.assertIn("You are a coding agent.", result[0]["content"])
                self.assertIn("Bash", result[0]["content"])
                self.assertIn("Run shell command", result[0]["content"])
                # The AGENT_INSTRUCTIONS / AGENT_TOOLS wraps survive the
                # system→user conversion (they were applied to the content
                # before the role rewrite).
                self.assertIn("<<<BEGIN_AGENT_INSTRUCTIONS>>>", result[0]["content"])
                self.assertIn("<<<BEGIN_AGENT_TOOLS>>>", result[0]["content"])
                self.assertEqual(result[1]["role"], "assistant")
                self.assertEqual(result[2]["role"], "user")
                # The recency block lands on the live user turn: the live
                # ask is wrapped in USER_REQUEST at the front, followed by
                # the reminder. USER_MESSAGE is reserved for prior user
                # turns (there are none here).
                self.assertIn("Reminder", result[2]["content"])
                self.assertIn("Hello", result[2]["content"])
                self.assertIn("<<<BEGIN_USER_REQUEST>>>", result[2]["content"])
                self.assertNotIn("<<<BEGIN_USER_MESSAGE>>>", result[2]["content"])
                self.assertLess(
                    result[2]["content"].index("<<<BEGIN_USER_REQUEST>>>"),
                    result[2]["content"].index("Reminder"),
                )

    def test_change_system_to_user_with_user_front_mode(self):
        """When PROMPT_MODE='user_front' AND CHANGE_SYSTEM_TO_USER is on,
        user_front injection still wraps the LAST user message with tools,
        and the converted-from-system content sits at the head with the
        stub assistant between."""
        with patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True):
            with patch.object(proxy, "_PROMPT_MODE", "user_front"):
                messages = [
                    {"role": "system", "content": "You are a coding agent."},
                    {"role": "user", "content": "Hello"},
                ]
                tools_text = "Bash: Run shell command"
                result = proxy.translate_history_and_apply_prompt(messages, tools_text)
                self.assertEqual(len(result), 3)
                self.assertEqual(result[0]["role"], "user")
                self.assertEqual(result[0]["content"], "You are a coding agent.")
                self.assertEqual(result[1]["role"], "assistant")
                self.assertEqual(result[2]["role"], "user")
                # user_front markers wrap the last user message
                self.assertIn("<<<BEGIN_USER_REQUEST>>>", result[2]["content"])
                self.assertIn("Hello", result[2]["content"])
                self.assertIn("Bash", result[2]["content"])

    def test_change_system_to_user_with_list_content(self):
        """Some clients send system content as a list of content-blocks.
        Conversion should flatten to a string."""
        with patch.object(proxy, "_CHANGE_SYSTEM_TO_USER", True):
            with patch.object(proxy, "_PROMPT_MODE", "user_front"):
                messages = [
                    {"role": "system", "content": [
                        {"type": "text", "text": "Block 1"},
                        {"type": "text", "text": "Block 2"},
                    ]},
                    {"role": "user", "content": "Hello"},
                ]
                result = proxy.translate_history_and_apply_prompt(messages, "")
                self.assertEqual(result[0]["role"], "user")
                self.assertIn("Block 1", result[0]["content"])
                self.assertIn("Block 2", result[0]["content"])


class TestUsageOverride(unittest.TestCase):
    """The proxy must override upstream's prompt_tokens with a local estimate
    of the translated conversation. Some upstreams truncate server-side and
    only count what they actually sent to the model, so their prompt_tokens
    does not grow monotonically with the agent's conversation length and is
    unusable for context tracking. completion_tokens is left to upstream
    when present."""

    def _fake_upstream_response(self, prompt_tokens, completion_tokens, content):
        class FakeResp:
            status_code = 200

            def json(self_inner):
                return {
                    "choices": [{"message": {"content": content}}],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                    },
                }

        return FakeResp()

    def test_prompt_tokens_overridden_with_local_estimate(self):
        long_user_msg = "x" * 4000  # ~1333 tokens at the chars/3 estimate
        ollama_request = {
            "model": "GenAI",
            "messages": [
                {"role": "user", "content": long_user_msg},
            ],
            "stream": False,
        }

        client = proxy.app.test_client()
        with patch.object(proxy.requests, "post") as mock_post:
            mock_post.return_value = self._fake_upstream_response(
                prompt_tokens=5,
                completion_tokens=11,
                content="ok",
            )
            with patch.object(proxy, "save_debug_file"):
                resp = client.post(
                    "/api/chat",
                    data=json.dumps(ollama_request),
                    content_type="application/json",
                )

        self.assertEqual(resp.status_code, 200)
        lines = [line for line in resp.get_data(as_text=True).split("\n") if line.strip()]
        self.assertTrue(lines)
        done_chunk = json.loads(lines[-1])
        self.assertTrue(done_chunk.get("done"))
        # Upstream said 5; the proxy must report a much larger count derived
        # from the translated conversation, which contains the 4000-char user
        # message plus cooperative-prompt scaffolding and markers.
        self.assertGreater(done_chunk["prompt_eval_count"], 100)
        self.assertNotEqual(done_chunk["prompt_eval_count"], 5)
        # completion_tokens passes through from upstream when present.
        self.assertEqual(done_chunk["eval_count"], 11)

    def test_completion_tokens_falls_back_to_estimate_when_missing(self):
        """When upstream omits completion_tokens, fall back to local estimate.
        prompt_tokens still always overridden."""
        ollama_request = {
            "model": "GenAI",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }

        class FakeResp:
            status_code = 200

            def json(self_inner):
                return {
                    "choices": [{"message": {"content": "y" * 400}}],
                    "usage": {"prompt_tokens": 1},
                }

        client = proxy.app.test_client()
        with patch.object(proxy.requests, "post") as mock_post:
            mock_post.return_value = FakeResp()
            with patch.object(proxy, "save_debug_file"):
                resp = client.post(
                    "/api/chat",
                    data=json.dumps(ollama_request),
                    content_type="application/json",
                )

        self.assertEqual(resp.status_code, 200)
        lines = [line for line in resp.get_data(as_text=True).split("\n") if line.strip()]
        done_chunk = json.loads(lines[-1])
        self.assertGreater(done_chunk["eval_count"], 50)


class TestConfigHelpers(unittest.TestCase):
    """URL-base normalization and model-tag stripping that back the
    passthrough + discovery behavior."""

    def test_normalize_strips_full_chat_url(self):
        self.assertEqual(
            proxy._normalize_api_base("https://host.example/v1/chat/completions"),
            "https://host.example",
        )

    def test_normalize_strips_bare_chat_completions(self):
        self.assertEqual(
            proxy._normalize_api_base("https://host.example/chat/completions"),
            "https://host.example",
        )

    def test_normalize_strips_trailing_v1(self):
        self.assertEqual(
            proxy._normalize_api_base("https://host.example/v1"), "https://host.example"
        )

    def test_normalize_root_unchanged(self):
        self.assertEqual(
            proxy._normalize_api_base("https://host.example/"), "https://host.example"
        )

    def test_normalize_preserves_extra_prefix(self):
        # A non-/v1 path prefix is kept; only the known suffixes are stripped.
        self.assertEqual(
            proxy._normalize_api_base("https://host.example/openai/v1/chat/completions"),
            "https://host.example/openai",
        )

    def test_chat_and_models_urls_derive_from_base(self):
        base = proxy._normalize_api_base("https://host.example/v1/chat/completions")
        self.assertEqual(base + "/v1/chat/completions", "https://host.example/v1/chat/completions")
        self.assertEqual(base + "/v1/models", "https://host.example/v1/models")

    def test_strip_model_tag_removes_latest(self):
        self.assertEqual(proxy._strip_model_tag("gpt-4:latest"), "gpt-4")

    def test_strip_model_tag_leaves_untagged(self):
        self.assertEqual(proxy._strip_model_tag("gpt-4"), "gpt-4")

    def test_strip_model_tag_keeps_non_latest_colon(self):
        self.assertEqual(proxy._strip_model_tag("ns:model"), "ns:model")


class TestModelPassthrough(unittest.TestCase):
    """The proxy forwards the requested model (minus :latest) upstream, not a
    fixed id; it falls back to DEFAULT_MODEL_NAME only when none was sent."""

    def _capture_forwarded(self, ollama_request):
        captured = {}

        class FakeResp:
            status_code = 200

            def json(self_inner):
                return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

        def fake_post(url, headers=None, json=None, **kwargs):
            captured["model"] = json.get("model")
            return FakeResp()

        client = proxy.app.test_client()
        with patch.object(proxy.requests, "post", side_effect=fake_post):
            with patch.object(proxy, "save_debug_file"):
                client.post(
                    "/api/chat",
                    data=json.dumps(ollama_request),
                    content_type="application/json",
                )
        return captured.get("model")

    def test_requested_model_passes_through_stripped(self):
        forwarded = self._capture_forwarded(
            {"model": "gpt-4:latest", "messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(forwarded, "gpt-4")

    def test_missing_model_falls_back_to_default(self):
        forwarded = self._capture_forwarded(
            {"messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(forwarded, proxy.DEFAULT_MODEL_NAME)


if __name__ == "__main__":
    unittest.main()
