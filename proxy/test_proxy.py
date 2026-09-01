"""Unit tests for proxy.py pure helpers.

Run inside the proxy container:
    docker compose run --rm proxy python -m unittest test_proxy.py
"""

import io
import json
import os
import tempfile
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

    def test_missing_arguments_lifted_when_name_is_known_tool(self):
        """Issue #118: models sometimes spell args at the top level instead
        of nested under `arguments`. When the `name` matches a tool we
        actually exposed for this turn, lift the remaining top-level keys
        into `arguments` rather than leaking the block into chat."""
        text = (
            'Here we go:\n'
            '```json\n'
            '{"name": "bash", "command": "ls -la", "description": "list files"}\n'
            '```\n'
            'Done.'
        )
        payloads, clean = proxy.extract_tool_calls_and_text(
            text, available_tool_names={"bash", "read", "write"}
        )
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["name"], "bash")
        self.assertEqual(
            payloads[0]["arguments"],
            {"command": "ls -la", "description": "list files"},
        )
        self.assertNotIn("```json", clean)
        self.assertIn("Here we go:", clean)
        self.assertIn("Done.", clean)

    def test_missing_arguments_not_lifted_when_name_is_unknown(self):
        """The lift is gated on the name matching a currently-available
        tool — instructional prose like ` ```json\\n{"name": "foo", ...}\\n``` `
        for a tool we never exposed must still leak into chat (model was
        describing JSON, not invoking)."""
        text = (
            '```json\n'
            '{"name": "no_such_tool", "command": "ls"}\n'
            '```'
        )
        payloads, clean = proxy.extract_tool_calls_and_text(
            text, available_tool_names={"bash", "read"}
        )
        self.assertEqual(payloads, [])
        # The block stays in clean_text — model gets to see its own prose.
        self.assertIn("no_such_tool", clean)

    def test_missing_arguments_not_lifted_with_no_tools_passed(self):
        """Defensive: when `available_tool_names` is empty/None (legacy
        callers, edge cases), the lift never fires — preserves prior
        behaviour for any code path that hasn't been updated yet."""
        text = (
            '```json\n'
            '{"name": "bash", "command": "ls"}\n'
            '```'
        )
        payloads, clean = proxy.extract_tool_calls_and_text(text)
        self.assertEqual(payloads, [])
        self.assertIn("bash", clean)

    def test_correctly_shaped_block_with_arguments_is_untouched_by_lift(self):
        """When `arguments` is already present, lifting is a no-op — a
        well-formed call must extract exactly as before even if other
        top-level keys happen to be there."""
        text = (
            '```json\n'
            '{"name": "bash", "arguments": {"command": "ls"}, "extra": "ignored"}\n'
            '```'
        )
        payloads, _ = proxy.extract_tool_calls_and_text(
            text, available_tool_names={"bash"}
        )
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["arguments"], {"command": "ls"})
        # The stray top-level key is NOT folded into arguments.
        self.assertNotIn("extra", payloads[0]["arguments"])


class TestCollectToolNames(unittest.TestCase):
    """The small helper that feeds `available_tool_names` into the lift gate."""

    def test_handles_function_envelope(self):
        tools = [
            {"function": {"name": "bash", "description": "..."}},
            {"function": {"name": "read", "description": "..."}},
        ]
        self.assertEqual(proxy._collect_tool_names(tools), {"bash", "read"})

    def test_handles_bare_name_shape(self):
        tools = [{"name": "bash"}, {"name": "read"}]
        self.assertEqual(proxy._collect_tool_names(tools), {"bash", "read"})

    def test_drops_empty_and_non_string_names(self):
        tools = [
            {"function": {"name": "bash"}},
            {"function": {"name": ""}},
            {"function": {}},
            {"function": {"name": 42}},
            "not a dict",
            None,
        ]
        self.assertEqual(proxy._collect_tool_names(tools), {"bash"})

    def test_none_or_empty_returns_empty_set(self):
        self.assertEqual(proxy._collect_tool_names(None), set())
        self.assertEqual(proxy._collect_tool_names([]), set())


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


class TestListContentFlattening(unittest.TestCase):
    """Regression: some clients send `content` as a list of content-blocks
    (multimodal user turns, or the AI SDK structuring an assistant turn as
    parts) instead of a plain string. Left as a list it crashed the proxy
    with a 500 in three places: assistant `.strip()`, the system/user `+=`
    concatenation, and the token-estimate `"\\n".join(...)`. Every inbound
    message's content is now flattened via `_flatten_content_to_str` before
    those paths run. These tests pin that contract and the no-crash behavior
    in every prompt mode."""

    def test_flatten_passes_str_through_unchanged(self):
        self.assertEqual(proxy._flatten_content_to_str("hello"), "hello")
        self.assertEqual(proxy._flatten_content_to_str(""), "")

    def test_flatten_joins_block_dicts_on_text(self):
        blocks = [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]
        self.assertEqual(proxy._flatten_content_to_str(blocks), "first\n\nsecond")

    def test_flatten_handles_bare_string_blocks(self):
        self.assertEqual(proxy._flatten_content_to_str(["a", "b"]), "a\n\nb")

    def test_flatten_skips_blocks_without_text(self):
        # Non-text blocks (e.g. image_url) carry no `text`; they drop out
        # rather than crashing or emitting "None".
        blocks = [
            {"type": "text", "text": "caption"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]
        self.assertEqual(proxy._flatten_content_to_str(blocks), "caption")

    def test_flatten_coerces_other_types_via_str(self):
        self.assertEqual(proxy._flatten_content_to_str(42), "42")

    def test_assistant_list_content_does_not_crash(self):
        # The exact shape that produced "'list' object has no attribute
        # 'strip'": a prior assistant turn whose content is a parts list.
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "earlier reply"}]},
            {"role": "user", "content": "again"},
        ]
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            out = proxy.translate_history_and_apply_prompt(msgs, "")
        assistant = [m for m in out if m["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]["content"], "earlier reply")

    def test_user_multimodal_list_content_does_not_crash(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "say hello"}]}]
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            out = proxy.translate_history_and_apply_prompt(msgs, "")
        user = [m for m in out if m["role"] == "user"]
        self.assertTrue(user)
        self.assertIn("say hello", user[-1]["content"])

    def test_user_front_mode_list_content_does_not_crash(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "say hello"}]}]
        with patch.object(proxy, "_PROMPT_MODE", "user_front"):
            out = proxy.translate_history_and_apply_prompt(msgs, "")
        self.assertIn("say hello", out[-1]["content"])

    def test_passthrough_mode_list_content_preserved_verbatim(self):
        # In passthrough the translator returns messages verbatim (list
        # content stays a list); the flatten safety net lives at the
        # token-estimate join instead, exercised by _flatten directly above.
        blocks = [{"type": "text", "text": "say hello"}]
        msgs = [{"role": "user", "content": blocks}]
        with patch.object(proxy, "_PROMPT_MODE", "passthrough"):
            out = proxy.translate_history_and_apply_prompt(msgs, "")
        self.assertEqual(out[0]["content"], blocks)


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

    def test_shipped_detail_tools_are_task_and_skill(self):
        # The detail-tools list is never an env var. It is now the
        # `detail_tools` key of proxy/tool-guidance.json (user-editable data,
        # loaded at import); this asserts the SHIPPED default, which is what
        # the rest of the suite renders against.
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
        # Tools content: JSON envelope (the tool-call body shape and the
        # complete-block requirement from issue #121) + no-fabricated-
        # results. Phrasing tightened in #121 to ban abbreviated/partial
        # fences and require valid JSON \escape on backslashes.
        self.assertIn('"name": "<tool>"', operating)
        self.assertIn('"arguments": {...}', operating)
        self.assertIn("COMPLETE ```json", operating)
        self.assertIn("JSON-escaped", operating)
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
        # Issue #118: per-item shape and the `priority` string enum reach
        # recency so the model stops trying ints / omitting the key / using
        # `description` instead of `content`. Locate the todowrite bullet
        # and assert only against its own body so we don't catch matches in
        # neighbouring tool entries.
        todo_start = c.index("- todowrite(todos)")
        todo_end = c.find("\n- ", todo_start + 1)
        if todo_end == -1:
            todo_end = c.index("]", todo_start)
        todo_block = c[todo_start:todo_end]
        self.assertIn("{content, status, priority}", todo_block)
        self.assertIn("'high'", todo_block)
        self.assertIn("'medium'", todo_block)
        self.assertIn("'low'", todo_block)
        self.assertIn("NOT an int", todo_block)

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

    def test_guidance_map_covers_known_opencode_tools(self):
        """The `_HYBRID_TOOL_GUIDANCE` map — the `tools` key of the shipped
        proxy/tool-guidance.json, loaded at import — is the union of tools
        harness knows about, including situational/optional ones opencode ships
        only when enabled (`websearch`, `lsp`, `apply_patch`, `question`,
        `repo_clone`/`repo_overview`, `plan-enter`/`plan-exit`). The cost
        of a stale entry is zero (it never renders unless the tool is
        passed for the turn), and the cost of missing an entry the moment
        a tool starts shipping is a bare signature with no failure-mode
        hint — so the map errs toward broader coverage. This test is the
        canary that flags accidental removal from the shipped file."""
        required = {
            "apply_patch", "bash", "edit", "glob", "grep", "lsp",
            "plan-enter", "plan-exit", "question", "read", "repo_clone",
            "repo_overview", "skill", "task", "todowrite", "webfetch",
            "websearch", "write",
        }
        self.assertTrue(
            required.issubset(set(proxy._HYBRID_TOOL_GUIDANCE.keys())),
            "missing keys in _HYBRID_TOOL_GUIDANCE: "
            f"{sorted(required - set(proxy._HYBRID_TOOL_GUIDANCE.keys()))}",
        )

    def test_optional_tool_renders_guidance_when_passed(self):
        """`websearch` is opencode-disabled by default and only ships when
        the agent env sets OPENCODE_ENABLE_EXA=1. When it IS in the tools
        list, the recency block must render its guidance line — exercising
        the same code path as for always-shipped tools like `bash`."""
        tools = self.tools + [{"function": {
            "name": "websearch",
            "description": "Search the web.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "type": {"type": "string"},
                },
                "required": ["query"],
            },
        }}]
        tools_text = proxy.format_tools_to_text(tools)
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            out = proxy.translate_history_and_apply_prompt(
                self.user_msgs, tools_text, tools=tools,
            )
        c = out[-1]["content"]
        self.assertIn(
            "- websearch(query, [type]) — Live web search via the session's "
            "web search provider",
            c,
        )
        # Year-framing reminder, the most common misuse.
        self.assertIn("Use the current year in queries", c)

    def test_optional_tool_not_in_block_when_absent(self):
        """An entry in `_HYBRID_TOOL_GUIDANCE` for a tool the agent isn't
        currently passing must NOT render — the per-turn block only shows
        what's actually available. Guards against the registry causing
        per-turn token regressions on stripped-down toolsets."""
        c = self._translate()[-1]["content"]
        # `self.tools` is the baseline 4-tool set (bash/todowrite/task/skill).
        # None of the optional-tool guidance strings should appear, even
        # though all those entries exist in the map.
        for absent in (
            "websearch", "apply_patch", "lsp", "plan-enter", "plan-exit",
            "question", "repo_clone", "repo_overview",
        ):
            self.assertNotIn(
                f"- {absent}", c,
                f"unexpected guidance for absent tool {absent!r} in recency",
            )

    def test_mcp_tool_renders_guidance_from_recency_map(self):
        """MCP tools arrive keyed `<server>_<tool>` (opencode's MCP naming, NOT
        the `mcp__serena__*` form the mock response fixtures use). Their guidance
        is NOT in `_HYBRID_TOOL_GUIDANCE` (that map is opencode's own tools); it
        loads at startup from HARNESS_MCP_TOOL_RECENCY into `_MCP_TOOL_RECENCY`,
        which `_format_tool_entries` consults as a fallback. When such a tool is
        in the inbound tools list its terse line must render at recency, keyed by
        the `<server>_<tool>` form."""
        tools = self.tools + [{"function": {
            "name": "serena_find_symbol",
            "description": "Find a symbol via the language server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name_path": {"type": "string"},
                    "relative_path": {"type": "string"},
                    "include_body": {"type": "boolean"},
                },
                "required": ["name_path"],
            },
        }}]
        tools_text = proxy.format_tools_to_text(tools)
        recency = {"serena_find_symbol": "Locate a symbol by `name_path`."}
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"), \
                patch.object(proxy, "_MCP_TOOL_RECENCY", recency):
            out = proxy.translate_history_and_apply_prompt(
                self.user_msgs, tools_text, tools=tools,
            )
        c = out[-1]["content"]
        self.assertIn("Locate a symbol by `name_path`", c)
        # The bare `mcp__serena__*` form must NOT be how the key is matched.
        self.assertIn("serena_find_symbol", c)

    def test_setup_mcp_tool_recency_parses_env(self):
        """`_setup_mcp_tool_recency` loads HARNESS_MCP_TOOL_RECENCY (a JSON
        `<server>_<tool>` -> str map) and keeps only str->non-empty-str entries;
        unset/empty/unparsable/non-dict yields an empty map (MCP tools then
        render as bare signatures, the same graceful degradation as no entry)."""
        cases = [
            ('{"serena_find_symbol": "do the thing", "a_b": 5, "c_d": ""}',
             {"serena_find_symbol": "do the thing"}),
            ("", {}),
            ("not json", {}),
            ("[1, 2, 3]", {}),
            ("{}", {}),
        ]
        for raw, expected in cases:
            with patch.dict(os.environ, {"HARNESS_MCP_TOOL_RECENCY": raw}):
                proxy._setup_mcp_tool_recency()
                self.assertEqual(proxy._MCP_TOOL_RECENCY, expected)
        # Restore the module default so later tests see a clean map.
        proxy._MCP_TOOL_RECENCY = {}

    def test_cwd_echoed_in_environment_when_system_has_working_directory(self):
        """When the inbound opencode system prompt carries a
        `Working directory: <path>` line in its `<env>` block, the recency
        Environment bullet echoes that path inline so "this folder"/"here"
        resolve concretely. Without this, the upstream's pretrained sense
        of its own sandbox path (e.g. `/home/bard`) has answered file-
        system questions instead of the real bind-mounted CWD."""
        msgs = [
            {"role": "system", "content": (
                "You are opencode.\n"
                "<env>\n"
                "  Working directory: /c/Users/handel.sim/Documents/ENC\n"
                "  Platform: linux\n"
                "</env>\n"
            )},
            {"role": "user", "content": "tell me about what is in this folder"},
        ]
        tools_text = proxy.format_tools_to_text(self.tools)
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            out = proxy.translate_history_and_apply_prompt(
                msgs, tools_text, tools=self.tools,
            )
        c = out[-1]["content"]
        self.assertIn(
            "The working directory for this turn is "
            "`/c/Users/handel.sim/Documents/ENC`",
            c,
        )
        # The aliases the user is most likely to use must be tied to the
        # echoed path so the model resolves them concretely.
        self.assertIn('"this folder"', c)
        self.assertIn('"here"', c)

    def test_cwd_clause_absent_when_system_has_no_working_directory(self):
        """No `Working directory:` line in the inbound system prompt (a
        non-opencode upstream, or a future opencode rename) => the
        Environment bullet falls back to the prior wording. Graceful
        degradation, never a hard fail."""
        msgs = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "do the thing"},
        ]
        tools_text = proxy.format_tools_to_text(self.tools)
        with patch.object(proxy, "_PROMPT_MODE", "hybrid"):
            out = proxy.translate_history_and_apply_prompt(
                msgs, tools_text, tools=self.tools,
            )
        c = out[-1]["content"]
        self.assertNotIn("The working directory for this turn is", c)
        # Environment still renders the load-bearing facts (host bind-mount,
        # reproducibility) — only the CWD clause drops.
        self.assertIn("- Environment:", c)
        self.assertIn("mounted from the host", c)

    def test_honesty_filesystem_clause_unconditional(self):
        """The Honesty addition (filesystem-claims must come from tool
        results) renders even when no CWD is found — it's load-bearing
        whether or not the positive anchor was extractable. The companion
        "don't name a remembered training path" clause was removed per
        user feedback on issue #110; only the positive filesystem-claim
        rule remains. Same baseline message set as `_translate()` (no
        `Working directory:` in `self.user_msgs`)."""
        c = self._translate()[-1]["content"]
        honesty = c[c.index("- Honesty:"):c.index("- Environment:")]
        self.assertIn("must come from a tool result", honesty)
        # Companion training-path clause is gone — verify it stays gone so
        # nobody re-adds it without re-opening the conversation.
        self.assertNotIn("training", honesty)
        self.assertNotIn("/home/<name>", honesty)
        self.assertNotIn("/sandbox", honesty)


class TestExtractWorkingDirectory(unittest.TestCase):
    """`_extract_working_directory` reads the host CWD out of opencode's
    `<env>` block in the inbound system prompt so the recency builder can
    echo it. Missing/unparsable => None, and the Environment line falls
    back to its prior wording."""

    def test_extracts_typical_opencode_env_block(self):
        text = (
            "You are opencode.\n"
            "<env>\n"
            "  Working directory: /home/user/project\n"
            "  Platform: linux\n"
            "</env>\n"
        )
        self.assertEqual(
            proxy._extract_working_directory(text),
            "/home/user/project",
        )

    def test_extracts_windows_style_host_path(self):
        text = "<env>\n  Working directory: /c/Users/foo/bar baz\n</env>"
        self.assertEqual(
            proxy._extract_working_directory(text),
            "/c/Users/foo/bar baz",
        )

    def test_no_marker_returns_none(self):
        self.assertIsNone(
            proxy._extract_working_directory("You are a helpful assistant."),
        )

    def test_empty_and_none_inputs_return_none(self):
        self.assertIsNone(proxy._extract_working_directory(""))
        self.assertIsNone(proxy._extract_working_directory(None))

    def test_empty_path_after_marker_returns_none(self):
        # The model shouldn't echo an empty path — a `Working directory:`
        # line with no value is no better than no marker at all.
        self.assertIsNone(
            proxy._extract_working_directory("<env>\n  Working directory:   \n</env>"),
        )


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
        positional order — opencode sends it directly."""
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
        request_body = {
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
                    "/v1/chat/completions",
                    data=json.dumps(request_body),
                    content_type="application/json",
                )

        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.get_data(as_text=True))
        # Upstream said 5; the proxy must report a much larger count derived
        # from the translated conversation, which contains the 4000-char user
        # message plus cooperative-prompt scaffolding and markers.
        self.assertGreater(body["usage"]["prompt_tokens"], 100)
        self.assertNotEqual(body["usage"]["prompt_tokens"], 5)
        # completion_tokens passes through from upstream when present.
        self.assertEqual(body["usage"]["completion_tokens"], 11)

    def test_completion_tokens_falls_back_to_estimate_when_missing(self):
        """When upstream omits completion_tokens, fall back to local estimate.
        prompt_tokens still always overridden."""
        request_body = {
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
                    "/v1/chat/completions",
                    data=json.dumps(request_body),
                    content_type="application/json",
                )

        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.get_data(as_text=True))
        self.assertGreater(body["usage"]["completion_tokens"], 50)


class TestEmptyResponseDetection(unittest.TestCase):
    """Issue #117. Some upstreams return a well-formed response with
    finish_reason=stop, 0 completion_tokens, and no content/tool_calls —
    silently short-circuiting before generation when something in the
    most-recent message trips a content/safety filter. When a `bash`-style
    shell tool is available in the inbound tools, the proxy emits a no-op
    `pwd` call to it (and NO substitute assistant text — a tool-only turn is
    well-formed) so opencode (a) executes the tool and (b) re-invokes the
    model with the tool result as the new recency, displacing the trigger
    out of the hot slot and letting the conversation continue without user
    intervention. Only when no shell tool is exposed does it fall back to a
    minimal `"Understood."` text in the assistant slot."""

    _BASH_TOOL = {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "required": ["command", "description"],
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
    }

    def _post(self, target_json, tools=None):
        request_body = {
            "model": "GenAI",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }
        if tools is not None:
            request_body["tools"] = tools

        class FakeResp:
            status_code = 200

            def json(self_inner):
                return target_json

        client = proxy.app.test_client()
        with patch.object(proxy.requests, "post") as mock_post:
            mock_post.return_value = FakeResp()
            with patch.object(proxy, "save_debug_file"):
                resp = client.post(
                    "/v1/chat/completions",
                    data=json.dumps(request_body),
                    content_type="application/json",
                )
        return resp

    def _body(self, resp):
        return json.loads(resp.get_data(as_text=True))

    def _content(self, body):
        return (body["choices"][0]["message"].get("content") or "")

    def _tool_calls(self, body):
        return body["choices"][0]["message"].get("tool_calls") or []

    def _finish_reason(self, body):
        return body["choices"][0].get("finish_reason")

    def test_empty_content_with_bash_emits_rescue_pwd_call(self):
        """The flagship issue #117 path: empty upstream content + bash is
        available → emit a no-op `pwd` call via the bash tool and NO
        substitute assistant text (a tool-only turn is well-formed), so
        finish_reason is `tool_calls` and the agent continues the turn by
        executing the (read-only) command."""
        resp = self._post(
            {
                "choices": [{
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {"role": "assistant", "content": ""},
                }],
                "usage": {"prompt_tokens": 3352, "completion_tokens": 0,
                          "total_tokens": 3352},
            },
            tools=[self._BASH_TOOL],
        )
        self.assertEqual(resp.status_code, 200)
        body = self._body(resp)
        # Tool-only rescue: assistant text stays empty (no "Understood.").
        self.assertEqual(self._content(body).strip(), "")
        self.assertNotIn("Understood.", self._content(body))
        tcs = self._tool_calls(body)
        self.assertEqual(len(tcs), 1, f"expected 1 rescue tool call, got {tcs}")
        self.assertEqual(tcs[0]["function"]["name"], "bash")
        # The args must be a read-only pwd invocation — anything else
        # (e.g. an empty command, or a state-modifying command) would
        # contradict the "inconsequential" design.
        self.assertEqual(
            json.loads(tcs[0]["function"]["arguments"]),
            {"command": "pwd", "description": "Print working directory"},
        )
        self.assertEqual(self._finish_reason(body), "tool_calls")
        content = self._content(body)
        self.assertNotIn("[harness proxy]", content)
        self.assertNotIn("finish_reason", content)

    def test_empty_content_without_rescue_tool_falls_back_to_text(self):
        """When no shell tool is in the inbound tools, the rescue still
        emits "Understood." in the assistant slot (the upstream unsticks on
        the user's next prompt), but no fabricated tool call is emitted and
        the finish_reason stays `stop`."""
        resp = self._post(
            {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": ""},
                }],
                "usage": {"completion_tokens": 0},
            },
            tools=[],
        )
        body = self._body(resp)
        self.assertEqual(self._content(body).strip(), "Understood.")
        self.assertEqual(self._tool_calls(body), [])
        self.assertEqual(self._finish_reason(body), "stop")

    def test_rescue_tool_matches_capitalized_bash(self):
        """Claude Code names the tool `Bash`; the selector matches
        case-insensitively so the rescue still finds it."""
        cap_tool = json.loads(json.dumps(self._BASH_TOOL))
        cap_tool["function"]["name"] = "Bash"
        resp = self._post(
            {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": ""},
                }],
                "usage": {"completion_tokens": 0},
            },
            tools=[cap_tool],
        )
        body = self._body(resp)
        tcs = self._tool_calls(body)
        self.assertEqual(len(tcs), 1)
        # The emitted name must echo the inbound name exactly, so opencode
        # can route the call.
        self.assertEqual(tcs[0]["function"]["name"], "Bash")
        self.assertEqual(
            json.loads(tcs[0]["function"]["arguments"]),
            {"command": "pwd", "description": "Print working directory"},
        )
        self.assertEqual(self._finish_reason(body), "tool_calls")

    def test_whitespace_only_content_treated_as_empty(self):
        """A response whose only content is whitespace still leaves the
        agent stalled — must trigger the rescue substitution."""
        resp = self._post(
            {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "   \n\t  "},
                }],
                "usage": {"completion_tokens": 0},
            },
            tools=[self._BASH_TOOL],
        )
        body = self._body(resp)
        # Tool-only rescue: assistant text stays empty (no "Understood.").
        self.assertEqual(self._content(body).strip(), "")
        self.assertNotIn("Understood.", self._content(body))
        self.assertEqual(len(self._tool_calls(body)), 1)

    def test_real_content_does_not_trigger_rescue(self):
        """Normal responses must pass through unchanged — no rescue text
        appended, no fabricated tool call."""
        resp = self._post(
            {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hello world"},
                }],
                "usage": {"completion_tokens": 2},
            },
            tools=[self._BASH_TOOL],
        )
        body = self._body(resp)
        content = self._content(body)
        self.assertIn("hello world", content)
        self.assertNotIn("Understood.", content)
        self.assertEqual(self._tool_calls(body), [])

    def test_tool_only_turn_does_not_trigger_rescue(self):
        """Tool-call-only turns have empty assistant text by design —
        must not be mistaken for the stalled-empty case. Critically, if
        the model's own response includes a bash call, the rescue must
        NOT append a second (spurious) pwd call."""
        tool_block = (
            'Calling a tool.\n'
            '```json\n'
            '{"name": "bash", "arguments": {"command": "ls", '
            '"description": "list files"}}\n'
            '```'
        )
        request_body = {
            "model": "GenAI",
            "messages": [{"role": "user", "content": "list the files"}],
            "tools": [self._BASH_TOOL],
            "stream": False,
        }

        class FakeResp:
            status_code = 200

            def json(self_inner):
                return {
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": tool_block},
                    }],
                    "usage": {"completion_tokens": 5},
                }

        client = proxy.app.test_client()
        with patch.object(proxy.requests, "post") as mock_post:
            mock_post.return_value = FakeResp()
            with patch.object(proxy, "save_debug_file"):
                resp = client.post(
                    "/v1/chat/completions",
                    data=json.dumps(request_body),
                    content_type="application/json",
                )

        self.assertEqual(resp.status_code, 200)
        body = self._body(resp)
        tcs = self._tool_calls(body)
        # Exactly the model's own `ls` call; no extra rescue `pwd`.
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "bash")
        self.assertEqual(
            json.loads(tcs[0]["function"]["arguments"])["command"], "ls"
        )
        self.assertNotIn("Understood.", self._content(body))

    def test_no_choices_emits_rescue(self):
        """Pathological 'no choices' response — agent still stalls, so still
        substitute the rescue."""
        resp = self._post(
            {"choices": [], "usage": {"completion_tokens": 0}},
            tools=[self._BASH_TOOL],
        )
        body = self._body(resp)
        # Tool-only rescue: assistant text stays empty (no "Understood.").
        self.assertEqual(self._content(body).strip(), "")
        self.assertNotIn("Understood.", self._content(body))
        self.assertEqual(len(self._tool_calls(body)), 1)

    def test_rescue_text_helper_returns_minimal_text(self):
        """Unit-test the rescue text helper directly so phrasing changes
        require an intentional test update."""
        text = proxy._empty_response_rescue_text()
        self.assertEqual(text, "Understood.")
        self.assertNotIn("[harness proxy]", text)

    def test_select_rescue_tool_picks_bash_and_emits_pwd(self):
        """Unit-test the rescue tool selector directly: when `bash` is
        present it picks it and emits a read-only pwd call."""
        payload = proxy._select_rescue_tool({"bash"})
        self.assertIsNotNone(payload)
        self.assertEqual(payload["name"], "bash")
        self.assertEqual(
            payload["arguments"],
            {"command": "pwd", "description": "Print working directory"},
        )

    def test_select_rescue_tool_returns_none_when_unavailable(self):
        """No shell tool in the set → None, caller falls back to text-only.
        Crucially, `todowrite` does NOT count as a shell rescue — we picked
        bash specifically and won't silently substitute another tool."""
        self.assertIsNone(proxy._select_rescue_tool({"todowrite", "read", "edit"}))
        self.assertIsNone(proxy._select_rescue_tool(set()))
        self.assertIsNone(proxy._select_rescue_tool(None))


class TestMalformedToolCallRetry(unittest.TestCase):
    """Issue #121. When the upstream emits a ```json tool-call attempt that
    fails to extract — either a fence opener with no parseable JSON body
    (the `\\`\\`\\`json_parse_or_id:todowrite}` shape that ended the turn
    silently), or a brace-balanced JSON object whose strings carry an
    invalid `\\escape` — the proxy appends a corrective `[assistant(<bad>),
    user(<correction>)]` pair to the conversation and re-POSTs upstream
    ONCE. The retry is invisible to opencode: a successful retry replaces
    the bad attempt; a failed retry falls through to the existing
    bleed/empty-rescue path."""

    _BASH_TOOL = {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "required": ["command", "description"],
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
    }

    _WRITE_TOOL = {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write to a file.",
            "parameters": {
                "type": "object",
                "required": ["filePath", "content"],
                "properties": {
                    "filePath": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        },
    }

    _TODOWRITE_TOOL = {
        "type": "function",
        "function": {
            "name": "todowrite",
            "description": "Maintain a todo list.",
            "parameters": {
                "type": "object",
                "required": ["todos"],
                "properties": {"todos": {"type": "array"}},
            },
        },
    }

    # --- _diagnose_failed_tool_call unit tests -----------------------------

    def test_diagnose_empty_response(self):
        self.assertIsNone(proxy._diagnose_failed_tool_call(""))

    def test_diagnose_no_fence_returns_none(self):
        self.assertIsNone(
            proxy._diagnose_failed_tool_call("plain prose, no fence here.")
        )

    def test_diagnose_malformed_fence_exact_issue_121_shape(self):
        """The exact shape that ended the turn silently in issue #121:
        ```json fence opener followed by a non-`{` character. The entire
        response is the broken fence — high-confidence malformed_fence."""
        self.assertEqual(
            proxy._diagnose_failed_tool_call(
                "```json_parse_or_id:todowrite}"
            ),
            "malformed_fence",
        )

    def test_diagnose_malformed_fence_with_trailing_whitespace(self):
        self.assertEqual(
            proxy._diagnose_failed_tool_call(
                "  ```json_parse_or_id:foo}\n\n"
            ),
            "malformed_fence",
        )

    def test_diagnose_malformed_fence_truncated_after_opener(self):
        """Fence opener then EOF (model stopped mid-output). Still a
        botched tool call worth retrying."""
        self.assertEqual(
            proxy._diagnose_failed_tool_call("```json"),
            "malformed_fence",
        )

    def test_diagnose_malformed_escape_with_invalid_x_escape(self):
        """Brace-balanced JSON whose `content` carries `\\x1e` — JSON spec
        rejects it. High-confidence malformed_escape (model attempted a
        write-style call and only the backslash escaping is wrong)."""
        text = '```json\n{"name":"write","arguments":{"filePath":"/tmp/x","content":"\\x1e"}}\n```'
        self.assertEqual(
            proxy._diagnose_failed_tool_call(text),
            "malformed_escape",
        )

    def test_diagnose_malformed_escape_with_invalid_a_escape(self):
        text = '```json\n{"name":"write","arguments":{"content":"bell:\\a"}}\n```'
        self.assertEqual(
            proxy._diagnose_failed_tool_call(text),
            "malformed_escape",
        )

    def test_diagnose_prose_with_json_example_is_not_retried(self):
        """Prose that mentions ```json should NOT trigger a retry — the
        model wasn't trying to call a tool. The conservative gating on
        malformed_fence requires the stripped response to START with the
        fence, so prose before it disqualifies the trigger."""
        text = (
            "Here's how a tool call looks: ```json {wrong shape}\n"
            "but you should use the actual schema."
        )
        self.assertIsNone(proxy._diagnose_failed_tool_call(text))

    def test_diagnose_valid_json_wrong_shape_is_not_retried(self):
        """A ```json block whose JSON parses fine but doesn't match the
        {name, arguments} shape (e.g. a model describing JSON) is left to
        bleed into chat, not retried."""
        text = '```json\n{"foo": "bar"}\n```'
        self.assertIsNone(proxy._diagnose_failed_tool_call(text))

    def test_diagnose_other_json_parse_error_is_not_retried(self):
        """Brace-unbalanced or shape-broken JSON that ISN'T a bad-escape
        falls through to bleed — the retry trigger is narrow to JSON-spec
        `Invalid \\escape` errors."""
        text = '```json\n{"name": "foo", missing colon}\n```'
        self.assertIsNone(proxy._diagnose_failed_tool_call(text))

    # --- Full HTTP path: bad → retry → recovered -------------------------

    def _post_with_sequential_responses(self, responses, tools=None,
                                        messages=None, capture=None):
        """Drive the catch_all flow with a queue of `responses` — each
        FakeResp is dequeued on successive `requests.post` calls. Returns
        (flask_response, post_call_count)."""
        request_body = {
            "model": "GenAI",
            "messages": messages or [{"role": "user", "content": "hello"}],
            "stream": False,
        }
        if tools is not None:
            request_body["tools"] = tools

        calls = {"count": 0, "payloads": []}

        def fake_post(url, headers=None, json=None, **kwargs):
            calls["count"] += 1
            calls["payloads"].append(json)
            if capture is not None:
                capture.append(json)
            try:
                target = responses.pop(0)
            except IndexError:
                self.fail("upstream called more times than expected")
            return target

        client = proxy.app.test_client()
        with patch.object(proxy.requests, "post", side_effect=fake_post):
            with patch.object(proxy, "save_debug_file"):
                resp = client.post(
                    "/v1/chat/completions",
                    data=json.dumps(request_body),
                    content_type="application/json",
                )
        return resp, calls

    @staticmethod
    def _resp(content):
        class FakeResp:
            status_code = 200

            def json(self_inner):
                return {
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
        return FakeResp()

    def _body(self, resp):
        return json.loads(resp.get_data(as_text=True))

    def _content(self, body):
        return body["choices"][0]["message"].get("content") or ""

    def _tool_calls(self, body):
        return body["choices"][0]["message"].get("tool_calls") or []

    def test_malformed_fence_recovered_on_retry(self):
        """The exact issue #121 shape: first response is the bare broken
        fence, second response carries the corrected todowrite call. The
        agent never sees the bad attempt — opencode gets only the retry's
        tool call."""
        good_block = (
            '```json\n'
            '{"name": "todowrite", "arguments": {"todos": []}}\n'
            '```'
        )
        responses = [
            self._resp("```json_parse_or_id:todowrite}"),
            self._resp(good_block),
        ]
        resp, calls = self._post_with_sequential_responses(
            responses, tools=[self._TODOWRITE_TOOL]
        )
        self.assertEqual(calls["count"], 2, "must POST twice (bad + retry)")
        body = self._body(resp)
        tcs = self._tool_calls(body)
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "todowrite")
        joined = self._content(body)
        # The broken fence and the proxy's corrective scaffolding must
        # NOT bleed into the assistant content the user sees.
        self.assertNotIn("json_parse_or_id", joined)
        self.assertNotIn("harness proxy", joined)

    def test_malformed_escape_recovered_on_retry(self):
        """A write call with `\\x1e` in content parses-but-fails-strict.
        Retry response uses properly-escaped `\\\\x1e` and the file write
        succeeds via the retry — opencode never sees the bad attempt."""
        bad = (
            '```json\n'
            '{"name":"write","arguments":{"filePath":"/tmp/x.py","content":"a\\x1eb"}}\n'
            '```'
        )
        # Corrected: real backslashes JSON-escaped as `\\\\x1e`. In the
        # source-level JSON the model would emit, that's `\\x1e`.
        good = (
            '```json\n'
            '{"name":"write","arguments":{"filePath":"/tmp/x.py","content":"a\\\\x1eb"}}\n'
            '```'
        )
        responses = [self._resp(bad), self._resp(good)]
        resp, calls = self._post_with_sequential_responses(
            responses, tools=[self._WRITE_TOOL]
        )
        self.assertEqual(calls["count"], 2)
        body = self._body(resp)
        tcs = self._tool_calls(body)
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "write")
        self.assertEqual(
            json.loads(tcs[0]["function"]["arguments"])["content"], "a\\x1eb"
        )

    def test_retry_carries_assistant_bad_and_user_correction(self):
        """The augmented payload sent to upstream on retry must include
        the bad assistant turn AND a user-role corrective message — this
        is the signal the model uses to self-correct. We verify by
        capturing the upstream messages on the second POST."""
        bad_text = "```json_parse_or_id:todowrite}"
        good_block = (
            '```json\n{"name": "todowrite", "arguments": {"todos": []}}\n```'
        )
        captured: list = []
        responses = [self._resp(bad_text), self._resp(good_block)]
        self._post_with_sequential_responses(
            responses,
            tools=[self._TODOWRITE_TOOL],
            messages=[{"role": "user", "content": "make a todo list"}],
            capture=captured,
        )
        self.assertEqual(len(captured), 2)
        retry_messages = captured[1]["messages"]
        joined = "\n".join(m.get("content", "") for m in retry_messages)
        # The bad attempt must be in the conversation as an assistant
        # turn the model can see — that's the signal it gets to recognise
        # what failed.
        self.assertIn(bad_text, joined)
        # The corrective message must be present so the model knows
        # WHAT to fix.
        self.assertIn("harness proxy", joined)
        self.assertIn("Re-emit", joined)
        # Last upstream message ends in the corrective ask
        # (recency builder wraps it as the live USER_REQUEST).
        self.assertIn("emit only the corrected", retry_messages[-1]["content"])

    def test_retry_budget_exhausted_falls_through_to_bleed(self):
        """Both attempts produce the same malformed fence — retry budget
        is 1, so the proxy doesn't loop forever. The original bad text
        bleeds into chat (existing behaviour) and the turn ends; opencode
        + the user see the malformed output and can react."""
        bad_text = "```json_parse_or_id:todowrite}"
        responses = [self._resp(bad_text), self._resp(bad_text)]
        resp, calls = self._post_with_sequential_responses(
            responses, tools=[self._TODOWRITE_TOOL]
        )
        # Exactly two upstream calls — no third attempt.
        self.assertEqual(calls["count"], 2)
        body = self._body(resp)
        self.assertEqual(self._tool_calls(body), [])
        # Bleed: the bad text reaches opencode as assistant content.
        joined = self._content(body)
        self.assertIn("json_parse_or_id", joined)

    def test_retry_recovers_with_pure_text_response(self):
        """If the retry model gives up on tool-calling and just answers
        in prose, that's still a recovered response — use it (the user
        gets a meaningful answer instead of garbage), don't fall through
        to bleed."""
        bad_text = "```json_parse_or_id:todowrite}"
        prose_reply = "I cannot complete that task with the available tools."
        responses = [self._resp(bad_text), self._resp(prose_reply)]
        resp, calls = self._post_with_sequential_responses(
            responses, tools=[self._TODOWRITE_TOOL]
        )
        self.assertEqual(calls["count"], 2)
        body = self._body(resp)
        self.assertEqual(self._tool_calls(body), [])
        joined = self._content(body)
        self.assertIn(prose_reply, joined)
        self.assertNotIn("json_parse_or_id", joined)

    def test_real_tool_call_does_not_trigger_retry(self):
        """A normal response with a valid tool call must NOT trigger the
        retry path — only one upstream POST happens."""
        good_block = (
            '```json\n'
            '{"name": "bash", "arguments": {"command": "ls", '
            '"description": "list files"}}\n'
            '```'
        )
        responses = [self._resp(good_block)]
        resp, calls = self._post_with_sequential_responses(
            responses, tools=[self._BASH_TOOL]
        )
        self.assertEqual(calls["count"], 1)
        body = self._body(resp)
        tcs = self._tool_calls(body)
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["function"]["name"], "bash")

    def test_prose_with_json_example_does_not_trigger_retry(self):
        """Negative case: response with prose around a ```json example
        the model is describing (not a botched call). No retry; the
        block stays in clean_text as before (issue #115 precedent)."""
        text = (
            "Here is what a write call looks like:\n"
            "```json\n"
            '{"this is": "not a tool call"}\n'
            "```\n"
            "but use the actual schema instead."
        )
        responses = [self._resp(text)]
        resp, calls = self._post_with_sequential_responses(
            responses, tools=[self._WRITE_TOOL]
        )
        self.assertEqual(calls["count"], 1, "must NOT retry on described JSON")
        body = self._body(resp)
        self.assertEqual(self._tool_calls(body), [])
        joined = self._content(body)
        self.assertIn("not a tool call", joined)

    def test_empty_response_still_triggers_rescue_not_retry(self):
        """An empty upstream response (issue #117 shape) goes to the
        empty-response rescue, not the malformed-tool-call retry — the
        diagnose helper returns None for empty content.

        With a shell tool available the rescue is the `pwd` tool call alone
        and NO substitute assistant text, so `"Understood."` must NOT appear
        on the streaming path either (the positive tool-only assertions live
        in TestEmptyResponseDetection's non-streaming tests)."""
        responses = [
            self._resp(""),
        ]
        resp, calls = self._post_with_sequential_responses(
            responses, tools=[self._BASH_TOOL]
        )
        # Only one upstream POST — no retry was triggered.
        self.assertEqual(calls["count"], 1)
        body = self._body(resp)
        joined = self._content(body)
        self.assertNotIn("Understood.", joined)

    def test_retry_upstream_error_falls_through_to_bleed(self):
        """If the retry POST itself errors (timeout, non-2xx), the proxy
        falls back to the original bad response (bleed into chat) rather
        than crashing or hanging."""
        bad_text = "```json_parse_or_id:todowrite}"

        class FailingRetry:
            status_code = 502

            def json(self_inner):
                return {"error": "upstream broken"}

        responses = [self._resp(bad_text), FailingRetry()]
        resp, calls = self._post_with_sequential_responses(
            responses, tools=[self._TODOWRITE_TOOL]
        )
        self.assertEqual(calls["count"], 2)
        # Original response still returned 200 — the retry's failure is
        # invisible to opencode; bad text bleeds as before.
        self.assertEqual(resp.status_code, 200)
        body = self._body(resp)
        joined = self._content(body)
        self.assertIn("json_parse_or_id", joined)

    # --- Correction-message constants ------------------------------------

    def test_correction_messages_cover_both_kinds(self):
        self.assertIn("malformed_escape", proxy._RETRY_CORRECTION_MESSAGES)
        self.assertIn("malformed_fence", proxy._RETRY_CORRECTION_MESSAGES)

    def test_correction_message_falls_back_to_fence_for_unknown_kind(self):
        """Defensive: an unknown kind doesn't KeyError, it falls back to
        the malformed-fence corrective (the more general of the two)."""
        msg = proxy._build_retry_correction_message("not_a_kind")
        self.assertEqual(
            msg, proxy._RETRY_CORRECTION_MESSAGES["malformed_fence"]
        )


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


class TestModelPassthrough(unittest.TestCase):
    """The proxy forwards the requested model verbatim upstream, not a
    fixed id; it falls back to DEFAULT_MODEL_NAME only when none was sent."""

    def _capture_forwarded(self, request_body):
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
                    "/v1/chat/completions",
                    data=json.dumps(request_body),
                    content_type="application/json",
                )
        return captured.get("model")

    def test_requested_model_passes_through_verbatim(self):
        forwarded = self._capture_forwarded(
            {"model": "gpt-4:latest", "messages": [{"role": "user", "content": "hi"}],
             "stream": False}
        )
        self.assertEqual(forwarded, "gpt-4:latest")

    def test_missing_model_falls_back_to_default(self):
        forwarded = self._capture_forwarded(
            {"messages": [{"role": "user", "content": "hi"}], "stream": False}
        )
        self.assertEqual(forwarded, proxy.DEFAULT_MODEL_NAME)


class TestOpenAIEmission(unittest.TestCase):
    """OpenAI-compatible inbound: SSE / JSON emission and the catch_all
    dispatch on /v1/chat/completions. The wire shape here is the contract the
    opencode @ai-sdk/openai-compatible provider parses."""

    # --- pure emitters ------------------------------------------------------

    def _sse_chunks(self, lines):
        self.assertEqual(lines[-1], "data: [DONE]\n\n")
        out = []
        for ln in lines[:-1]:
            self.assertTrue(ln.startswith("data: ") and ln.endswith("\n\n"), repr(ln))
            out.append(json.loads(ln[len("data: "):].strip()))
        return out

    def test_sse_text_only_stream(self):
        lines = list(proxy.generate_openai_sse(
            "m", "hello", [], {"prompt_tokens": 3, "completion_tokens": 2}))
        chunks = self._sse_chunks(lines)
        # stable id + chunk object across the stream
        self.assertEqual({c["id"] for c in chunks}.__len__(), 1)
        for c in chunks:
            self.assertEqual(c["object"], "chat.completion.chunk")
            self.assertIsInstance(c["created"], int)
        self.assertEqual(chunks[0]["choices"][0]["delta"],
                         {"role": "assistant", "content": "hello"})
        fin = [c for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]]
        self.assertEqual(fin[0]["choices"][0]["finish_reason"], "stop")
        usage = [c for c in chunks if c.get("usage")]
        self.assertEqual(usage[0]["choices"], [])
        self.assertEqual(usage[0]["usage"],
                         {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})

    def test_sse_tool_calls_arguments_are_json_strings(self):
        payloads = [
            {"name": "bash", "arguments": {"command": "ls", "description": "list"}},
            {"name": "read", "arguments": {"path": "/x"}},
        ]
        chunks = self._sse_chunks(list(
            proxy.generate_openai_sse("m", "", payloads, None)))
        tc_delta = next(c for c in chunks
                        if c["choices"] and "tool_calls" in c["choices"][0]["delta"])
        tcs = tc_delta["choices"][0]["delta"]["tool_calls"]
        self.assertEqual([t["index"] for t in tcs], [0, 1])
        for t in tcs:
            self.assertTrue(t["id"])
            self.assertEqual(t["type"], "function")
            self.assertIsInstance(t["function"]["arguments"], str)
            json.loads(t["function"]["arguments"])  # parses to valid JSON
        self.assertEqual(json.loads(tcs[0]["function"]["arguments"]),
                         {"command": "ls", "description": "list"})
        fin = [c for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]]
        self.assertEqual(fin[0]["choices"][0]["finish_reason"], "tool_calls")
        # no usage chunk when usage is None
        self.assertFalse(any(c.get("usage") for c in chunks))

    def test_sse_tool_only_first_delta_has_role(self):
        chunks = self._sse_chunks(list(proxy.generate_openai_sse(
            "m", "", [{"name": "bash", "arguments": {"command": "pwd"}}], None)))
        first = next(c for c in chunks if c["choices"] and c["choices"][0]["delta"])
        self.assertEqual(first["choices"][0]["delta"].get("role"), "assistant")

    def test_build_openai_response_non_streaming(self):
        body = proxy.build_openai_response(
            "m", "answer",
            [{"name": "bash", "arguments": {"command": "ls"}}],
            {"prompt_tokens": 4, "completion_tokens": 1})
        self.assertEqual(body["object"], "chat.completion")
        msg = body["choices"][0]["message"]
        self.assertEqual(msg["content"], "answer")
        self.assertNotIn("index", msg["tool_calls"][0])  # index is streaming-only
        self.assertIsInstance(msg["tool_calls"][0]["function"]["arguments"], str)
        self.assertEqual(body["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(body["usage"]["total_tokens"], 5)

    def test_build_openai_response_text_only_omits_tool_calls(self):
        body = proxy.build_openai_response("m", "hi", [], {})
        msg = body["choices"][0]["message"]
        self.assertEqual(msg["content"], "hi")
        self.assertNotIn("tool_calls", msg)
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")

    # --- catch_all dispatch via the Flask test client -----------------------

    def _post(self, path, body):
        class FakeResp:
            status_code = 200

            def json(self_inner):
                return {"choices": [{"message": {"content": "Hello there"},
                                     "finish_reason": "stop"}], "usage": {}}

        client = proxy.app.test_client()
        with patch.object(proxy.requests, "post", side_effect=lambda *a, **k: FakeResp()):
            with patch.object(proxy, "save_debug_file"):
                return client.post(path, data=json.dumps(body),
                                   content_type="application/json")

    def test_v1_chat_completions_streams_sse(self):
        resp = self._post("/v1/chat/completions",
                          {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                           "stream": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.mimetype.startswith("text/event-stream"), resp.mimetype)
        text = resp.get_data(as_text=True)
        self.assertIn("chat.completion.chunk", text)
        self.assertIn("Hello there", text)
        self.assertTrue(text.rstrip().endswith("data: [DONE]"), text[-40:])

    def test_v1_chat_completions_non_stream_returns_json(self):
        resp = self._post("/v1/chat/completions",
                          {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                           "stream": False})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        body = json.loads(resp.get_data(as_text=True))
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["message"]["content"], "Hello there")

    def test_openai_error_envelope_on_upstream_failure(self):
        client = proxy.app.test_client()
        with patch.object(proxy.requests, "post",
                          side_effect=proxy.requests.RequestException("boom")):
            with patch.object(proxy, "save_debug_file"):
                resp = client.post(
                    "/v1/chat/completions",
                    data=json.dumps({"model": "m",
                                     "messages": [{"role": "user", "content": "hi"}]}),
                    content_type="application/json")
        body = json.loads(resp.get_data(as_text=True))
        # AI SDK requires error.message.
        self.assertIn("message", body["error"])
        self.assertIn("boom", body["error"]["message"])


class TestCooperativeToolSearch(unittest.TestCase):
    """Pure-function coverage for the cooperative tool-search build
    (HARNESS_TOOL_SEARCH). The serve loop (`_serve_meta_tools`) is exercised
    end-to-end by the docker proxy suite under CI; these cover the per-request
    registry rendering and dispatch the loop is built on."""

    TOOLS = [
        {"function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "number"},
                },
                "required": ["command"],
            },
        }},
        {"function": {
            "name": "read",
            "description": "Read a file from disk.",
            "parameters": {
                "type": "object",
                "properties": {"filePath": {"type": "string"}},
                "required": ["filePath"],
            },
        }},
    ]

    def test_tool_list_renders_one_line_per_tool(self):
        out = proxy._meta_tool_list(self.TOOLS)
        self.assertTrue(out.startswith("Available tools:\n"))
        self.assertIn("- bash(command, [timeout])", out)
        self.assertIn("Run a shell command.", out)
        self.assertIn("- read(filePath)", out)
        self.assertIn("Read a file from disk.", out)

    def test_tool_list_empty_when_no_tools(self):
        self.assertEqual(proxy._meta_tool_list(None), "No tools available.")
        self.assertEqual(proxy._meta_tool_list([]), "No tools available.")

    def test_tool_search_matches_name(self):
        out = proxy._meta_tool_search("bash", self.TOOLS)
        self.assertIn("Tool: bash(command, [timeout])", out)
        self.assertIn("Run a shell command.", out)
        self.assertNotIn("read(filePath)", out)

    def test_tool_search_matches_description_case_insensitive(self):
        out = proxy._meta_tool_search("FILE", self.TOOLS)
        self.assertIn("Tool: read(filePath)", out)
        self.assertNotIn("bash(command", out)

    def test_tool_search_empty_query_does_not_dump_catalog(self):
        out = proxy._meta_tool_search("", self.TOOLS)
        self.assertNotIn("Tool:", out)
        self.assertIn("tool_list()", out)

    def test_tool_search_no_match_points_at_tool_list(self):
        out = proxy._meta_tool_search("nonexistent_xyz", self.TOOLS)
        self.assertIn("No tools match", out)
        self.assertIn("tool_list()", out)

    def test_is_meta_tool_call_true_for_synthetic_name(self):
        self.assertTrue(
            proxy._is_meta_tool_call({"name": "tool_list"}, {"bash", "read"}))
        self.assertTrue(
            proxy._is_meta_tool_call({"name": "tool_search"}, {"bash"}))

    def test_is_meta_tool_call_yields_to_real_tool_of_same_name(self):
        # Safety yield: if opencode ever ships a real tool by this name, the
        # real one wins and the proxy forwards it untouched.
        self.assertFalse(
            proxy._is_meta_tool_call({"name": "tool_list"}, {"tool_list"}))

    def test_is_meta_tool_call_false_for_real_and_non_dict(self):
        self.assertFalse(proxy._is_meta_tool_call({"name": "bash"}, {"bash"}))
        self.assertFalse(proxy._is_meta_tool_call("not a dict", set()))

    def test_run_meta_tool_dispatch(self):
        self.assertEqual(
            proxy._run_meta_tool({"name": "tool_list", "arguments": {}}, self.TOOLS),
            proxy._meta_tool_list(self.TOOLS),
        )
        self.assertEqual(
            proxy._run_meta_tool(
                {"name": "tool_search", "arguments": {"query": "bash"}}, self.TOOLS),
            proxy._meta_tool_search("bash", self.TOOLS),
        )
        self.assertEqual(proxy._run_meta_tool({"name": "other"}, self.TOOLS), "")


class TestStateCheckRendering(unittest.TestCase):
    """`_format_tool_entries` marks state-mutating tools `[state-check]` and,
    when one is present, appends the orient-first rule to the legend. Driven by
    the `_MCP_STATE_CHECK_TOOLS` global (loaded from HARNESS_MCP_STATE_CHECK)."""

    def test_state_check_marker_and_orient_legend(self):
        sigs = [("host_build", ["target"], []), ("host_state", [], [])]
        with patch.object(proxy, "_MCP_STATE_CHECK_TOOLS", {"host_build"}), \
                patch.object(proxy, "_MCP_TOOL_RECENCY", {}), \
                patch.object(proxy, "_TOOL_SEARCH_ENABLED", False):
            out = proxy._format_tool_entries(sigs)
        self.assertIn("- host_build(target) [state-check]", out)
        self.assertIn("- host_state", out)
        self.assertNotIn("host_state(", out)  # zero-param tool renders bare
        self.assertIn("[state-check] mutates state", out)

    def test_no_orient_legend_without_state_check_tool(self):
        sigs = [("host_state", [], [])]
        with patch.object(proxy, "_MCP_STATE_CHECK_TOOLS", set()), \
                patch.object(proxy, "_MCP_TOOL_RECENCY", {}), \
                patch.object(proxy, "_TOOL_SEARCH_ENABLED", False):
            out = proxy._format_tool_entries(sigs)
        self.assertNotIn("[state-check]", out)
        self.assertNotIn("mutates state", out)

    def test_tool_search_legend_appended_when_enabled(self):
        sigs = [("host_state", [], [])]
        with patch.object(proxy, "_MCP_STATE_CHECK_TOOLS", set()), \
                patch.object(proxy, "_MCP_TOOL_RECENCY", {}), \
                patch.object(proxy, "_TOOL_SEARCH_ENABLED", True):
            out = proxy._format_tool_entries(sigs)
        self.assertIn("tool_list()", out)
        self.assertIn("tool_search(", out)


class TestSetupStateCheckAndToolSearch(unittest.TestCase):
    """Env parsing for the two new setup functions, mirroring
    `_setup_mcp_tool_recency`'s graceful-degradation contract."""

    def test_setup_state_check_parses_json_array(self):
        cases = [
            ('["a_b", "c_d"]', {"a_b", "c_d"}),
            ("", set()),
            ("not json", set()),
            ('{"a": 1}', set()),
            ("[]", set()),
            ('["a_b", 5, ""]', {"a_b"}),
        ]
        for raw, expected in cases:
            with patch.dict(os.environ, {"HARNESS_MCP_STATE_CHECK": raw}):
                proxy._setup_state_check_tools()
                self.assertEqual(proxy._MCP_STATE_CHECK_TOOLS, expected)
        proxy._MCP_STATE_CHECK_TOOLS = set()

    def test_setup_tool_search_truthy_values(self):
        for raw, expected in [
            ("1", True), ("true", True), ("yes", True), ("on", True),
            ("0", False), ("false", False), ("", False), ("nope", False),
        ]:
            with patch.dict(os.environ, {"HARNESS_TOOL_SEARCH": raw}):
                proxy._setup_tool_search()
                self.assertEqual(proxy._TOOL_SEARCH_ENABLED, expected)
        proxy._TOOL_SEARCH_ENABLED = False


class TestForceLoopbackGuard(unittest.TestCase):
    """HARNESS_FORCE_LOOPBACK refuses a non-loopback bind in host mode.

    Guards proxy.py:_validate_config. Containerless host mode has no egress
    firewall and fronts the upstream key with no auth of its own, so a
    regression that bound the proxy off-loopback would expose a keyed endpoint
    on the LAN. `harness host` sets HARNESS_FORCE_LOOPBACK=1; container mode
    never sets it and may bind 0.0.0.0 on purpose behind the firewall.
    """

    def _run_validate(self, host, force_val):
        # force_val: string to set HARNESS_FORCE_LOOPBACK to, or None to unset.
        # patch.dict restores os.environ on exit, so the pop is safe.
        overrides = {}
        if force_val is not None:
            overrides["HARNESS_FORCE_LOOPBACK"] = force_val
        with patch.object(proxy, "PROXY_HOST", host), \
                patch.dict(os.environ, overrides, clear=False):
            if force_val is None:
                os.environ.pop("HARNESS_FORCE_LOOPBACK", None)
            proxy._validate_config()

    def test_refuses_non_loopback_when_forced(self):
        for host in ("0.0.0.0", "192.168.1.10", "::"):
            for truthy in ("1", "true", "yes"):
                with self.assertRaises(SystemExit):
                    self._run_validate(host, truthy)

    def test_allows_loopback_when_forced(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            self._run_validate(host, "1")  # must not raise

    def test_allows_any_host_when_not_forced(self):
        # Container mode: var unset or falsey, a 0.0.0.0 bind is allowed.
        self._run_validate("0.0.0.0", None)
        self._run_validate("0.0.0.0", "0")
        self._run_validate("0.0.0.0", "false")


class TestForceUtf8Stdio(unittest.TestCase):
    """_force_utf8_stdio makes stdout/stderr UTF-8 so a non-ASCII print cannot
    crash the proxy.

    Regression for the Windows host-mode crash: with stdout redirected to the
    host-mode logfile, Python uses the legacy cp1252 code page, and printing the
    U+2192 arrow in the startup banner (`sys->user:`) raised
    `UnicodeEncodeError: 'charmap' codec can't encode character '\\u2192'`,
    killing the proxy at startup.
    """

    @staticmethod
    def _cp1252_stream():
        # What Python hands proxy.py for a redirected stdout on Windows: a text
        # stream over bytes, encoding cp1252 with strict errors.
        return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")

    def test_arrow_crashes_on_cp1252_baseline(self):
        # The exact failure the user hit: the arrow does not fit in cp1252.
        out = self._cp1252_stream()
        with self.assertRaises(UnicodeEncodeError):
            out.write("sys→user")
            out.flush()

    def test_reconfigures_to_utf8_and_arrow_survives(self):
        out = self._cp1252_stream()
        err = self._cp1252_stream()
        with patch.object(proxy.sys, "stdout", out), \
                patch.object(proxy.sys, "stderr", err):
            proxy._force_utf8_stdio()
            # Same writes that crashed before must now succeed on both streams.
            print("sys→user", file=proxy.sys.stdout, flush=True)
            print("err→", file=proxy.sys.stderr, flush=True)

        self.assertEqual(out.encoding.lower().replace("-", ""), "utf8")
        self.assertEqual(err.encoding.lower().replace("-", ""), "utf8")
        # The arrow round-trips as UTF-8 bytes in the underlying buffer.
        self.assertIn("→".encode("utf-8"), out.buffer.getvalue())
        self.assertIn("→".encode("utf-8"), err.buffer.getvalue())

    def test_noop_when_stream_lacks_reconfigure(self):
        # Pre-3.7 / exotic streams without reconfigure() must be tolerated, not
        # crash the proxy before it can serve.
        class _Bare:
            pass

        with patch.object(proxy.sys, "stdout", _Bare()), \
                patch.object(proxy.sys, "stderr", _Bare()):
            proxy._force_utf8_stdio()  # must not raise


class TestReminderTemplateFile(unittest.TestCase):
    """The hybrid recency reminder's prose is user-owned DATA loaded from a
    file (`_setup_reminder_template`), not a literal in proxy.py, so an
    operator can reword it and `harness restart` without a code change.

    These tests cover the loader's contract. The reminder's actual WORDING is
    asserted by TestHybridConsolidatedRecency against the shipped default in
    proxy/reminder.md — reword that file and those tests are what fails."""

    @classmethod
    def setUpClass(cls):
        # The shipped default, so tearDown can put the global back for the
        # rest of the suite (which asserts on the real prose).
        if proxy._REMINDER_TEMPLATE is None:
            proxy._setup_reminder_template()
        cls.shipped = proxy._REMINDER_TEMPLATE

    def tearDown(self):
        proxy._REMINDER_TEMPLATE = self.shipped

    def _load(self, text, tmpdir=None):
        """Write `text` to reminder.md in a temp dir, point INSTALL_ROOT at
        that dir, load it, and return (template, stdout, path)."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "reminder.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            buf = io.StringIO()
            with patch.dict(os.environ, {"INSTALL_ROOT": td}), \
                    patch("sys.stdout", buf):
                proxy._setup_reminder_template()
            return proxy._REMINDER_TEMPLATE, buf.getvalue(), path

    # -- loading -----------------------------------------------------------

    def test_install_root_overrides_the_adjacent_default(self):
        # `harness host` runs proxy.py straight from the clone, where there is
        # no bind-mount to swap reminder.md — INSTALL_ROOT is how the user's
        # editable copy (seeded next to .env at the install root) is reached in
        # that mode. It names the DIRECTORY, and the copy keeps the tracked
        # basename, so one variable serves this file and tool-guidance.json.
        tmpl, _, _ = self._load("just this")
        self.assertEqual(tmpl, "just this")

    def test_leading_comment_block_is_stripped(self):
        # The file documents its own tokens in a top-of-file HTML comment;
        # that is authoring metadata, not prompt text, so it must not reach
        # the model (and must not burn tokens every turn).
        tmpl, _, _ = self._load(
            "<!-- docs for the editor\n   spanning lines -->\n[Reminder body]\n"
        )
        self.assertEqual(tmpl, "[Reminder body]")

    def test_only_a_leading_comment_is_stripped(self):
        # Anchored at the start: a `<!--` inside the prose is the user's text.
        tmpl, _, _ = self._load("[Reminder <!-- kept --> body]\n")
        self.assertEqual(tmpl, "[Reminder <!-- kept --> body]")

    def test_trailing_newlines_are_stripped(self):
        # Editors add a final newline; the reminder must end at `]` so the
        # rendered prompt is unchanged by how the file was saved.
        tmpl, _, _ = self._load("[Reminder]\n\n\n")
        self.assertEqual(tmpl, "[Reminder]")

    def test_shipped_default_loads_and_carries_every_token(self):
        # Guards proxy/reminder.md itself: dropping a token there would
        # silently strand host-OS / cwd / tool entries out of the prompt.
        env_no_var = {k: v for k, v in os.environ.items()
                      if k != "INSTALL_ROOT"}
        with patch.dict(os.environ, env_no_var, clear=True):
            # With no env override the default is reminder.md sitting next to
            # proxy.py. Assert that layout-independently: in a checkout that
            # dir is proxy/, but the proxy_test suite runs this inside the
            # container where the Dockerfile COPYs proxy.py to /app, so a
            # hardcoded proxy/ parent would (and did) fail there.
            default_path = proxy._reminder_template_path()
            self.assertEqual(os.path.basename(default_path), "reminder.md")
            self.assertEqual(
                os.path.dirname(default_path),
                os.path.dirname(os.path.abspath(proxy.__file__)))
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                proxy._setup_reminder_template()
        tmpl = proxy._REMINDER_TEMPLATE
        self.assertNotEqual(tmpl, proxy._REMINDER_FALLBACK)
        for token in (proxy._REMINDER_TOKEN_HOST_OS,
                      proxy._REMINDER_TOKEN_CWD,
                      proxy._REMINDER_TOKEN_TOOL_ENTRIES):
            self.assertIn(token, tmpl)
        self.assertTrue(tmpl.startswith("[Reminder"))
        self.assertTrue(tmpl.endswith("]"))

    # -- degradation -------------------------------------------------------

    def test_missing_file_falls_back_loudly(self):
        # The realistic cause is an INSTALL_ROOT with no reminder.md in it
        # (an un-seeded install), or a bind-mount whose source is missing.
        # Falling back keeps the tool-call envelope alive; `[!]` makes it
        # visible.
        with tempfile.TemporaryDirectory() as td:
            missing = os.path.join(td, "reminder.md")
            buf = io.StringIO()
            with patch.dict(os.environ, {"INSTALL_ROOT": td}), \
                    patch("sys.stdout", buf):
                proxy._setup_reminder_template()
            self.assertEqual(proxy._REMINDER_TEMPLATE, proxy._REMINDER_FALLBACK)
            self.assertIn("[!]", buf.getvalue())
            self.assertIn(missing, buf.getvalue())

    def test_directory_at_the_path_falls_back(self):
        # Exactly what docker leaves behind for a missing mount source: a
        # DIRECTORY named reminder.md mounted over the file.
        with tempfile.TemporaryDirectory() as td:
            os.mkdir(os.path.join(td, "reminder.md"))
            buf = io.StringIO()
            with patch.dict(os.environ, {"INSTALL_ROOT": td}), \
                    patch("sys.stdout", buf):
                proxy._setup_reminder_template()
            self.assertEqual(proxy._REMINDER_TEMPLATE, proxy._REMINDER_FALLBACK)
            self.assertIn("[!]", buf.getvalue())

    def test_empty_and_comment_only_files_fall_back(self):
        for raw in ("", "   \n\n", "<!-- only the header -->\n"):
            tmpl, out, _ = self._load(raw)
            self.assertEqual(tmpl, proxy._REMINDER_FALLBACK, repr(raw))
            self.assertIn("[!]", out)

    def test_fallback_keeps_the_tool_call_envelope(self):
        # The fallback is deliberately NOT a copy of the prose (it would
        # drift), but it must keep the mechanically load-bearing part or tool
        # calling breaks outright.
        self.assertIn('"name"', proxy._REMINDER_FALLBACK)
        self.assertIn('"arguments"', proxy._REMINDER_FALLBACK)
        self.assertIn(proxy._REMINDER_TOKEN_TOOL_ENTRIES,
                      proxy._REMINDER_FALLBACK)

    # -- substitution ------------------------------------------------------

    def test_tokens_are_substituted_into_the_rendered_reminder(self):
        self._load(
            "[Reminder host={{HOST_OS}} cwd={{CWD}} tools={{TOOL_ENTRIES}}]"
        )
        with patch.object(proxy, "_HOST_OS", "macos"):
            out = proxy.build_cooperative_prompt_hybrid_reminder(
                "do it", [("read", ["path"], [])],
                working_directory="/w/x")
        self.assertIn("host= (host OS: macos)", out)
        self.assertIn("/w/x", out)
        self.assertIn("read(path)", out)
        self.assertNotIn("{{", out)

    def test_unknown_tokens_and_stray_braces_are_left_literal(self):
        # str.replace, not str.format/Template: a user edit must never be able
        # to raise, and the prose legitimately contains `{...}` and
        # backslashes.
        self._load('[Reminder {{NOPE}} {"a": {"b": 1}} \\n {}]')
        out = proxy.build_cooperative_prompt_hybrid_reminder("hi", [])
        self.assertIn("{{NOPE}}", out)
        self.assertIn('{"a": {"b": 1}}', out)

    def test_absent_optional_tokens_render_nothing_extra(self):
        # A user who deletes {{CWD}} / {{HOST_OS}} just loses those clauses;
        # the reminder still renders.
        self._load("[Reminder only {{TOOL_ENTRIES}}]")
        out = proxy.build_cooperative_prompt_hybrid_reminder(
            "hi", [], working_directory="/w/x")
        self.assertIn("[Reminder only", out)
        self.assertNotIn("/w/x", out)

    def test_builder_lazy_loads_when_main_never_ran(self):
        # Importing proxy.py (tests, host mode helpers) must not require
        # main(); the builder loads the template on first use.
        proxy._REMINDER_TEMPLATE = None
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            out = proxy.build_cooperative_prompt_hybrid_reminder("hi", [])
        self.assertIsNotNone(proxy._REMINDER_TEMPLATE)
        self.assertIn("[Reminder", out)



# ---------------------------------------------------------------------------
# ChatGPT backend-api
# ---------------------------------------------------------------------------


class _FakeStreamResp:
    """Stand-in for the streaming `requests.Response` the backend-api returns."""

    def __init__(self, lines, status_code=200, text="", headers=None):
        self._lines = lines
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.closed = False

    def iter_lines(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


def _sse(*objs):
    """Render dicts (or raw strings) as the `data: ` lines of an SSE body."""
    out = []
    for o in objs:
        out.append(o if isinstance(o, str) else "data: " + json.dumps(o))
    return out


class TestChatGPTFlatten(unittest.TestCase):
    """The stateless proxy re-sends the whole history each turn, so the
    backend-api's single user message has to carry all of it."""

    def test_single_message_passes_through_verbatim(self):
        # The common first turn must be byte-identical to the reference client.
        out = proxy._chatgpt_flatten_messages([{"role": "user", "content": "hello"}])
        self.assertEqual(out, "hello")

    def test_multi_turn_history_is_labeled_not_dropped(self):
        out = proxy._chatgpt_flatten_messages([
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ])
        self.assertIn("User: first", out)
        self.assertIn("Assistant: second", out)
        self.assertIn("User: third", out)
        # Order preserved.
        self.assertLess(out.index("first"), out.index("second"))
        self.assertLess(out.index("second"), out.index("third"))

    def test_blank_messages_are_dropped(self):
        out = proxy._chatgpt_flatten_messages([
            {"role": "system", "content": "   "},
            {"role": "user", "content": "only this"},
        ])
        self.assertEqual(out, "only this")

    def test_empty_history_is_empty_string(self):
        self.assertEqual(proxy._chatgpt_flatten_messages([]), "")

    def test_list_content_is_flattened_not_crashed(self):
        out = proxy._chatgpt_flatten_messages(
            [{"role": "user", "content": [{"type": "text", "text": "block"}]}]
        )
        self.assertIn("block", out)


class TestChatGPTCollectStream(unittest.TestCase):
    def test_message_delta_chunks_accumulate(self):
        resp = _FakeStreamResp(_sse(
            {"type": "message_delta", "delta": "Hel"},
            {"type": "message_delta", "delta": "lo "},
            {"type": "message_delta", "delta": "world"},
            "data: [DONE]",
        ))
        self.assertEqual(proxy._chatgpt_collect_stream(resp)[0], "Hello world")

    def test_cumulative_parts_take_only_the_new_suffix(self):
        # message.content.parts repeats everything so far on every event.
        resp = _FakeStreamResp(_sse(
            {"message": {"id": "m1", "content": {"parts": ["Hel"]}}},
            {"message": {"id": "m1", "content": {"parts": ["Hello"]}}},
            {"message": {"id": "m1", "content": {"parts": ["Hello world"]}}},
            "data: [DONE]",
        ))
        self.assertEqual(proxy._chatgpt_collect_stream(resp)[0], "Hello world")

    def test_done_terminates_the_stream(self):
        resp = _FakeStreamResp(_sse(
            {"type": "message_delta", "delta": "kept"},
            "data: [DONE]",
            {"type": "message_delta", "delta": "dropped"},
        ))
        self.assertEqual(proxy._chatgpt_collect_stream(resp)[0], "kept")

    def test_non_data_and_malformed_lines_are_skipped(self):
        resp = _FakeStreamResp([
            "",
            "event: ping",
            "data: {not json",
            'data: {"type": "message_delta", "delta": "ok"}',
            "data: [DONE]",
        ])
        self.assertEqual(proxy._chatgpt_collect_stream(resp)[0], "ok")

    def test_bytes_lines_are_decoded(self):
        resp = _FakeStreamResp([
            b'data: {"type": "message_delta", "delta": "bytes"}',
            b"data: [DONE]",
        ])
        self.assertEqual(proxy._chatgpt_collect_stream(resp)[0], "bytes")

    def test_data_without_a_space_is_still_an_event(self):
        # "data:{...}" is legal SSE; the reference client's startswith("data: ")
        # dropped it silently.
        resp = _FakeStreamResp([
            'data:{"type": "message_delta", "delta": "tight"}',
            "data:[DONE]",
        ])
        self.assertEqual(proxy._chatgpt_collect_stream(resp)[0], "tight")

    def test_event_count_separates_no_stream_from_an_empty_answer(self):
        # A 200 carrying a login page has zero events; a stream that really
        # said nothing has some. Downstream they must not look alike.
        html = _FakeStreamResp(["<!doctype html>", "<title>Sign in</title>"])
        self.assertEqual(proxy._chatgpt_collect_stream(html)[1], 0)
        empty = _FakeStreamResp(["data: [DONE]"])
        text, events, _ = proxy._chatgpt_collect_stream(empty)
        self.assertEqual(text, "")
        self.assertEqual(events, 1)

    def test_non_assistant_snapshot_cannot_clobber_the_answer(self):
        # "longest wins" applied blindly lets one long reasoning or tool
        # message replace the assistant's actual reply.
        resp = _FakeStreamResp(_sse(
            {"type": "message_delta", "delta": "Hello"},
            {"message": {"id": "m2", "author": {"role": "tool"},
                         "content": {"parts": ["a very long tool result indeed"]}}},
            {"type": "message_delta", "delta": " world"},
            "data: [DONE]",
        ))
        self.assertEqual(proxy._chatgpt_collect_stream(resp)[0], "Hello world")

    def test_non_text_content_type_snapshot_is_skipped(self):
        resp = _FakeStreamResp(_sse(
            {"type": "message_delta", "delta": "answer"},
            {"message": {"id": "m3", "author": {"role": "assistant"},
                         "content": {"content_type": "thoughts",
                                     "parts": ["let me think about this at length"]}}},
            "data: [DONE]",
        ))
        self.assertEqual(proxy._chatgpt_collect_stream(resp)[0], "answer")

    def test_unlabelled_snapshot_is_still_accepted(self):
        # The reference client filtered on neither role nor content_type;
        # dropping unlabelled events would break the shape it verified.
        resp = _FakeStreamResp(_sse(
            {"message": {"id": "m4", "content": {"parts": ["plain snapshot"]}}},
            "data: [DONE]",
        ))
        self.assertEqual(proxy._chatgpt_collect_stream(resp)[0], "plain snapshot")


class TestChatGPTPost(unittest.TestCase):
    def _post(self, lines, status_code=200, text="", resp_headers=None,
              base_url="https://chat.example.com"):
        captured = {}

        def fake_post(url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            captured["kwargs"] = kwargs
            return _FakeStreamResp(lines, status_code=status_code, text=text,
                                   headers=resp_headers)

        with patch.object(proxy, "CHATGPT_BASE_URL", base_url), \
             patch.object(proxy, "CHATGPT_STREAM_URL",
                          "https://chat.example.com/backend-api/conversation/stream"), \
             patch.object(proxy, "CHATGPT_MODEL_NAME", "gpt-5.6-terra"), \
             patch.object(proxy, "CHATGPT_COOKIE_STRING", "session=abc"), \
             patch.object(proxy.requests, "post", side_effect=fake_post):
            resp = proxy._chatgpt_post(
                {"model": "ignored-by-backend",
                 "messages": [{"role": "user", "content": "hi"}]}
            )
        return captured, resp

    def test_posts_to_the_hardcoded_stream_endpoint(self):
        captured, _ = self._post(_sse({"type": "message_delta", "delta": "x"},
                                      "data: [DONE]"))
        self.assertEqual(
            captured["url"],
            "https://chat.example.com/backend-api/conversation/stream",
        )
        self.assertTrue(captured["kwargs"].get("stream"))

    def test_payload_matches_the_backend_api_dialect(self):
        captured, _ = self._post(_sse("data: [DONE]"))
        body = captured["body"]
        self.assertEqual(body["action"], "next")
        self.assertEqual(body["model"], "gpt-5.6-terra")
        self.assertEqual(body["timezone"], "America/Chicago")
        self.assertEqual(body["timezone_offset_min"], 300)
        self.assertEqual(body["messages"][0]["author"], {"role": "user"})
        self.assertEqual(body["messages"][0]["content"]["content_type"], "text")
        self.assertEqual(body["messages"][0]["content"]["parts"], ["hi"])

    def test_no_server_side_conversation_state_is_reused(self):
        # The proxy is stateless and re-sends the full history, so carrying
        # conversation_id/parent_message_id forward would duplicate it.
        captured, _ = self._post(_sse("data: [DONE]"))
        self.assertNotIn("conversation_id", captured["body"])
        self.assertNotIn("parent_message_id", captured["body"])

    def test_headers_carry_cookie_origin_and_referer(self):
        captured, _ = self._post(_sse("data: [DONE]"))
        h = captured["headers"]
        self.assertEqual(h["Cookie"], "session=abc")
        self.assertEqual(h["Accept"], "text/event-stream")
        self.assertEqual(h["Origin"], "https://chat.example.com")
        self.assertEqual(h["Referer"], "https://chat.example.com/")
        self.assertIn("Chrome/152", h["User-Agent"])

    def test_returns_an_openai_shaped_response(self):
        _, resp = self._post(_sse({"type": "message_delta", "delta": "answer"},
                                  "data: [DONE]"))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(proxy.extract_assistant_content(body), "answer")
        self.assertEqual(proxy._extract_finish_reason(body), "stop")
        self.assertEqual(body["model"], "gpt-5.6-terra")
        # `.text` has to be real JSON: the error paths dump it.
        self.assertEqual(json.loads(resp.text)["object"], "chat.completion")

    def test_error_status_is_surfaced_with_the_upstream_body(self):
        _, resp = self._post([], status_code=403, text="cookie expired")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("cookie expired", resp.json()["error"]["message"])

    def test_redirects_are_not_followed(self):
        # requests drops the Cookie header across a redirect and turns the POST
        # into a GET, so a followed redirect lands unauthenticated and returns
        # a 200 login page.
        captured, _ = self._post(_sse("data: [DONE]"))
        self.assertIs(captured["kwargs"].get("allow_redirects"), False)

    def test_a_redirect_is_an_error_not_an_empty_answer(self):
        _, resp = self._post([], status_code=302, text="",
                             resp_headers={"Location": "/auth/login"})
        self.assertEqual(resp.status_code, 502)
        msg = resp.json()["error"]["message"]
        self.assertIn("redirected", msg)
        self.assertIn("/auth/login", msg)

    def test_a_200_that_is_not_an_event_stream_is_an_error(self):
        # Without this the agent gets an empty completion, hits the
        # empty-response rescue, and loops on the rescue tool forever.
        _, resp = self._post(["<!doctype html>", "<title>Sign in</title>"])
        self.assertEqual(resp.status_code, 502)
        self.assertIn("no SSE events", resp.json()["error"]["message"])

    def test_a_stream_that_genuinely_said_nothing_is_still_a_200(self):
        _, resp = self._post(_sse("data: [DONE]"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(proxy.extract_assistant_content(resp.json()), "")

    def test_origin_strips_a_path_prefix_from_the_base_url(self):
        captured, _ = self._post(_sse("data: [DONE]"),
                                 base_url="https://chat.example.com/api")
        self.assertEqual(captured["headers"]["Origin"], "https://chat.example.com")
        self.assertEqual(captured["headers"]["Referer"], "https://chat.example.com/")


class TestUpstreamPostRouting(unittest.TestCase):
    def test_openai_backend_posts_to_chat_url(self):
        seen = {}

        def fake_post(url, **kwargs):
            seen["url"] = url
            return _FakeStreamResp([])

        with patch.object(proxy, "PROXY_BACKEND", "openai"), \
             patch.object(proxy.requests, "post", side_effect=fake_post):
            proxy._upstream_post({}, {"model": "m", "messages": []})
        self.assertEqual(seen["url"], proxy.CHAT_URL)

    def test_chatgpt_backend_routes_to_the_chatgpt_client(self):
        with patch.object(proxy, "PROXY_BACKEND", "chatgpt"), \
             patch.object(proxy, "_chatgpt_post",
                          return_value="sentinel") as spy:
            out = proxy._upstream_post({}, {"model": "m", "messages": []})
        self.assertEqual(out, "sentinel")
        spy.assert_called_once()


class TestChatGPTEndToEnd(unittest.TestCase):
    """The chatgpt branch only replaces the outbound call; everything
    downstream (tool-call extraction, both emitters) must be unchanged."""

    def _request(self, lines, body):
        def fake_post(url, headers=None, json=None, **kwargs):
            return _FakeStreamResp(lines)

        client = proxy.app.test_client()
        with patch.object(proxy, "PROXY_BACKEND", "chatgpt"), \
             patch.object(proxy, "CHATGPT_MODEL_NAME", "gpt-5.6-terra"), \
             patch.object(proxy, "CHATGPT_COOKIE_STRING", "session=abc"), \
             patch.object(proxy.requests, "post", side_effect=fake_post), \
             patch.object(proxy, "save_debug_file"):
            return client.post(
                "/v1/chat/completions",
                data=json.dumps(body),
                content_type="application/json",
            )

    def test_plain_answer_reaches_the_client(self):
        resp = self._request(
            _sse({"type": "message_delta", "delta": "42"}, "data: [DONE]"),
            {"model": "gpt-5.6-terra", "stream": False,
             "messages": [{"role": "user", "content": "answer?"}]},
        )
        self.assertEqual(resp.status_code, 200)
        out = json.loads(resp.data)
        self.assertEqual(out["choices"][0]["message"]["content"], "42")

    def test_cooperative_tool_calls_are_still_extracted(self):
        fence = "```json\n" + json.dumps(
            {"name": "bash", "arguments": {"command": "pwd"}}
        ) + "\n```"
        resp = self._request(
            _sse({"type": "message_delta", "delta": fence}, "data: [DONE]"),
            {"model": "gpt-5.6-terra", "stream": False,
             "messages": [{"role": "user", "content": "run pwd"}],
             "tools": [{"type": "function", "function": {"name": "bash"}}]},
        )
        out = json.loads(resp.data)
        calls = out["choices"][0]["message"].get("tool_calls") or []
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "bash")

    def test_upstream_error_becomes_a_502_for_the_client(self):
        def fake_post(url, headers=None, json=None, **kwargs):
            return _FakeStreamResp([], status_code=401, text="bad cookie")

        client = proxy.app.test_client()
        with patch.object(proxy, "PROXY_BACKEND", "chatgpt"), \
             patch.object(proxy.requests, "post", side_effect=fake_post), \
             patch.object(proxy, "save_debug_file"):
            resp = client.post(
                "/v1/chat/completions",
                data=json.dumps({"model": "m", "stream": False,
                                 "messages": [{"role": "user", "content": "hi"}]}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 502)


class TestChatGPTModelCatalog(unittest.TestCase):
    def test_catalog_is_synthesized_without_calling_upstream(self):
        client = proxy.app.test_client()

        def boom(*a, **k):
            raise AssertionError("chatgpt backend must not GET a models endpoint")

        with patch.object(proxy, "PROXY_BACKEND", "chatgpt"), \
             patch.object(proxy, "CHATGPT_MODEL_NAME", "gpt-5.6-terra"), \
             patch.object(proxy.requests, "get", side_effect=boom):
            resp = client.get("/v1/models")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)["data"]
        self.assertEqual([m["id"] for m in data], ["gpt-5.6-terra"])

    def test_openai_backend_still_proxies_the_catalog(self):
        class FakeResp:
            status_code = 200
            text = '{"data": [{"id": "upstream-model"}]}'
            headers = {"Content-Type": "application/json"}

        client = proxy.app.test_client()
        with patch.object(proxy, "PROXY_BACKEND", "openai"), \
             patch.object(proxy.requests, "get", return_value=FakeResp()):
            resp = client.get("/v1/models")
        self.assertIn("upstream-model", resp.data.decode())


class TestChatGPTValidateConfig(unittest.TestCase):
    def test_chatgpt_requires_only_its_own_three_vars(self):
        with patch.object(proxy, "PROXY_BACKEND", "chatgpt"), \
             patch.object(proxy, "CHATGPT_BASE_URL", "https://chat.example.com"), \
             patch.object(proxy, "CHATGPT_MODEL_NAME", "gpt-5.6-terra"), \
             patch.object(proxy, "CHATGPT_COOKIE_STRING", "session=abc"), \
             patch.object(proxy, "PROXY_API_URL", ""), \
             patch.object(proxy, "PROXY_API_KEY", ""), \
             patch.object(proxy, "DEFAULT_MODEL_NAME", ""), \
             patch("sys.stdout", io.StringIO()):
            proxy._validate_config()  # must not raise SystemExit

    def test_missing_cookie_is_fatal(self):
        with patch.object(proxy, "PROXY_BACKEND", "chatgpt"), \
             patch.object(proxy, "CHATGPT_BASE_URL", "https://chat.example.com"), \
             patch.object(proxy, "CHATGPT_MODEL_NAME", "gpt-5.6-terra"), \
             patch.object(proxy, "CHATGPT_COOKIE_STRING", ""), \
             patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit):
                proxy._validate_config()

    def test_unknown_backend_is_fatal(self):
        with patch.object(proxy, "PROXY_BACKEND", "gemini"), \
             patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit):
                proxy._validate_config()

    def test_openai_backend_still_requires_its_trio(self):
        with patch.object(proxy, "PROXY_BACKEND", "openai"), \
             patch.object(proxy, "PROXY_API_URL", ""), \
             patch.object(proxy, "PROXY_API_KEY", "sk-x"), \
             patch.object(proxy, "DEFAULT_MODEL_NAME", "m"), \
             patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit):
                proxy._validate_config()

    def test_passthrough_is_rejected_on_the_chatgpt_backend(self):
        # The backend-api has no tools field: passthrough would drop every
        # tool schema and tool result and look like it worked.
        with patch.object(proxy, "PROXY_BACKEND", "chatgpt"), \
             patch.object(proxy, "CHATGPT_BASE_URL", "https://chat.example.com"), \
             patch.object(proxy, "CHATGPT_MODEL_NAME", "gpt-5.6-terra"), \
             patch.object(proxy, "CHATGPT_COOKIE_STRING", "session=abc"), \
             patch.object(proxy, "_PROMPT_MODE", "passthrough"), \
             patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit):
                proxy._validate_config()

    def test_passthrough_is_still_fine_on_the_openai_backend(self):
        with patch.object(proxy, "PROXY_BACKEND", "openai"), \
             patch.object(proxy, "PROXY_API_URL", "https://api.example.com/v1"), \
             patch.object(proxy, "PROXY_API_KEY", "sk-x"), \
             patch.object(proxy, "DEFAULT_MODEL_NAME", "m"), \
             patch.object(proxy, "_PROMPT_MODE", "passthrough"), \
             patch("sys.stdout", io.StringIO()):
            proxy._validate_config()  # must not raise SystemExit

class TestToolGuidanceFile(unittest.TestCase):
    """The per-tool entries block's data — the legend, its two conditional
    sentences, the detail-tool list, and every tool's one-line guidance — is
    user-owned DATA loaded from proxy/tool-guidance.json, not literals in
    proxy.py, so an operator can retune a tool's one-liner and `harness
    restart` without a code change.

    These tests cover the loader's contract. The guidance WORDING is asserted
    by TestHybridConsolidatedRecency against the shipped default — reword that
    file and those tests are what fails.

    The load is per-section on purpose: the whole point of a hand-edited file
    with ~18 separately-pulled descriptions is that a typo in one of them must
    not blank the other seventeen."""

    @classmethod
    def setUpClass(cls):
        cls.shipped = (
            proxy._HYBRID_LEGEND,
            proxy._HYBRID_STATE_CHECK_NOTE,
            proxy._HYBRID_TOOL_SEARCH_NOTE,
            list(proxy._HYBRID_DETAIL_TOOLS),
            dict(proxy._HYBRID_TOOL_GUIDANCE),
        )

    def tearDown(self):
        (proxy._HYBRID_LEGEND, proxy._HYBRID_STATE_CHECK_NOTE,
         proxy._HYBRID_TOOL_SEARCH_NOTE, proxy._HYBRID_DETAIL_TOOLS,
         proxy._HYBRID_TOOL_GUIDANCE) = (
            self.shipped[0], self.shipped[1], self.shipped[2],
            list(self.shipped[3]), dict(self.shipped[4]))

    def _load(self, text):
        """Write `text` to a temp guidance file and return (values, warnings)."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "tool-guidance.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            return proxy._load_tool_guidance(path)

    def _setup(self, text):
        """As _load, but through _setup_tool_guidance so the globals and the
        printed output are what get asserted."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "tool-guidance.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            buf = io.StringIO()
            with patch.dict(os.environ, {"INSTALL_ROOT": td}), \
                    patch("sys.stdout", buf):
                proxy._setup_tool_guidance()
            return buf.getvalue(), path

    # --- path resolution ---------------------------------------------------

    def test_install_root_overrides_the_adjacent_default(self):
        # INSTALL_ROOT — which harness already exports for compose — names the
        # DIRECTORY holding both user-editable prompt files; each keeps its
        # tracked basename, so neither needs a path variable of its own.
        with patch.dict(os.environ, {"INSTALL_ROOT": "/x/root"}):
            self.assertEqual(proxy._tool_guidance_path(),
                             "/x/root/tool-guidance.json")
            self.assertEqual(proxy._reminder_template_path(),
                             "/x/root/reminder.md")

    def test_default_path_sits_next_to_proxy_py(self):
        # Layout-independent on purpose: in a checkout that dir is proxy/, but
        # the proxy_test suite runs inside the container where the Dockerfile
        # COPYs proxy.py to /app.
        env = {k: v for k, v in os.environ.items()
               if k != "INSTALL_ROOT"}
        with patch.dict(os.environ, env, clear=True):
            path = proxy._tool_guidance_path()
        self.assertEqual(os.path.basename(path), "tool-guidance.json")
        self.assertEqual(os.path.dirname(path),
                         os.path.dirname(os.path.abspath(proxy.__file__)))

    # --- the shipped default ----------------------------------------------

    def test_shipped_default_loads_every_section(self):
        # Guards proxy/tool-guidance.json itself: dropping a key there would
        # silently strand the legend or the whole guidance map out of the
        # prompt with only an `[!]` line to show for it.
        env = {k: v for k, v in os.environ.items()
               if k != "INSTALL_ROOT"}
        with patch.dict(os.environ, env, clear=True):
            values, warnings = proxy._load_tool_guidance(
                proxy._tool_guidance_path())
        self.assertEqual(warnings, [])
        self.assertNotEqual(values["legend"], proxy._HYBRID_LEGEND_FALLBACK)
        self.assertIn("Signature format: name(required, [optional])",
                      values["legend"])
        self.assertIn("[state-check]", values["state_check_note"])
        self.assertIn("tool_search(", values["tool_search_note"])
        self.assertEqual(values["detail_tools"], ["task", "skill"])
        self.assertGreaterEqual(len(values["tools"]), 18)
        for name, line in values["tools"].items():
            self.assertIsInstance(line, str, name)
            self.assertTrue(line.strip(), name)

    def test_shipped_default_readme_key_is_ignored(self):
        # The file documents itself in a `_README` key because JSON has no
        # comment syntax; it must never leak into the rendered block.
        with open(os.path.join(
                os.path.dirname(os.path.abspath(proxy.__file__)),
                "tool-guidance.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertIn("_README", raw)
        self.assertNotIn("_README", raw["tools"])
        values, _ = proxy._load_tool_guidance(proxy._tool_guidance_path())
        self.assertNotIn("_README", values)

    # --- per-section degradation ------------------------------------------

    def test_one_bad_description_does_not_cost_the_others(self):
        # This is the whole reason the format is structured: the descriptions
        # are pulled separately, so a typo in one is a one-entry loss.
        values, warnings = self._load(json.dumps({
            "tools": {"bash": "run a command", "edit": 5, "read": ""},
        }))
        self.assertEqual(values["tools"], {"bash": "run a command"})
        self.assertEqual(len(warnings), 1)
        self.assertIn("edit", warnings[0])
        self.assertIn("read", warnings[0])

    def test_a_missing_key_keeps_the_built_in_default(self):
        values, warnings = self._load(json.dumps({"tools": {"bash": "x"}}))
        self.assertEqual(warnings, [])
        self.assertEqual(values["legend"], proxy._HYBRID_LEGEND_FALLBACK)
        self.assertEqual(values["detail_tools"],
                         proxy._HYBRID_DETAIL_TOOLS_FALLBACK)

    def test_empty_notes_are_honoured_but_an_empty_legend_is_not(self):
        # Blanking a conditional sentence is a real edit ("stop saying that");
        # blanking the legend would leave the tool list unexplained.
        values, warnings = self._load(json.dumps({
            "legend": "   ", "state_check_note": "", "tool_search_note": "",
        }))
        self.assertEqual(values["legend"], proxy._HYBRID_LEGEND_FALLBACK)
        self.assertEqual(values["state_check_note"], "")
        self.assertEqual(values["tool_search_note"], "")
        self.assertEqual(len(warnings), 1)
        self.assertIn("legend", warnings[0])

    def test_wrong_types_fall_back_per_key_and_name_the_key(self):
        values, warnings = self._load(json.dumps({
            "legend": 42, "detail_tools": "task", "tools": ["bash"],
        }))
        self.assertEqual(values["legend"], proxy._HYBRID_LEGEND_FALLBACK)
        self.assertEqual(values["detail_tools"],
                         proxy._HYBRID_DETAIL_TOOLS_FALLBACK)
        self.assertEqual(values["tools"], {})
        joined = " ".join(warnings)
        for key in ("legend", "detail_tools", "tools"):
            self.assertIn(key, joined)

    def test_explicit_empty_detail_tools_is_honoured(self):
        values, warnings = self._load(json.dumps({"detail_tools": []}))
        self.assertEqual(values["detail_tools"], [])
        self.assertEqual(warnings, [])

    def test_non_string_detail_tool_entries_are_dropped(self):
        values, warnings = self._load(
            json.dumps({"detail_tools": ["task", 7, "", "skill"]}))
        self.assertEqual(values["detail_tools"], ["task", "skill"])
        self.assertEqual(len(warnings), 1)

    # --- whole-file failures ----------------------------------------------

    def test_malformed_json_names_line_and_column(self):
        values, warnings = self._load('{"tools": {"bash": "x",}}')
        self.assertEqual(values["tools"], {})
        self.assertEqual(values["legend"], proxy._HYBRID_LEGEND_FALLBACK)
        self.assertEqual(len(warnings), 1)
        self.assertIn("line", warnings[0])
        self.assertIn("column", warnings[0])

    def test_missing_file_falls_back_loudly(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "nope.json")
            values, warnings = proxy._load_tool_guidance(path)
        self.assertEqual(values["tools"], {})
        self.assertEqual(values["legend"], proxy._HYBRID_LEGEND_FALLBACK)
        self.assertIn(path, warnings[0])

    def test_directory_at_the_path_falls_back(self):
        # A compose bind-mount whose source is missing makes docker create a
        # DIRECTORY at the mount point.
        with tempfile.TemporaryDirectory() as td:
            values, warnings = proxy._load_tool_guidance(td)
        self.assertEqual(values["tools"], {})
        self.assertTrue(warnings)

    def test_non_object_top_level_falls_back(self):
        for text in ('[]', '"a string"', 'null', '3'):
            values, warnings = self._load(text)
            self.assertEqual(values["tools"], {}, text)
            self.assertTrue(warnings, text)

    def test_setup_logs_warnings_and_a_count(self):
        out, path = self._setup('{"tools": {"bash": "x", "edit": 5}}')
        self.assertIn("[!]", out)
        self.assertIn("edit", out)
        self.assertIn("[i] tool guidance: 1 tool(s)", out)
        self.assertIn(path, out)
        self.assertEqual(proxy._HYBRID_TOOL_GUIDANCE, {"bash": "x"})

    # --- end-to-end rendering ---------------------------------------------

    def test_edited_file_reaches_the_rendered_block(self):
        self._setup(json.dumps({
            "legend": "MY LEGEND.",
            "state_check_note": " MY STATE NOTE.",
            "tool_search_note": " MY SEARCH NOTE.",
            "tools": {"bash": "my bash line"},
        }))
        with patch.object(proxy, "_MCP_STATE_CHECK_TOOLS", {"bash"}), \
                patch.object(proxy, "_TOOL_SEARCH_ENABLED", True):
            out = proxy._format_tool_entries([("bash", ["command"], [])])
        self.assertIn("MY LEGEND. MY STATE NOTE. MY SEARCH NOTE.", out)
        self.assertIn("- bash(command) [state-check] — my bash line", out)

    def test_a_tool_dropped_from_the_file_renders_a_bare_signature(self):
        self._setup(json.dumps({"legend": "L.", "tools": {}}))
        with patch.object(proxy, "_MCP_TOOL_RECENCY", {}):
            out = proxy._format_tool_entries([("bash", ["command"], [])])
        self.assertIn("- bash(command)", out)
        self.assertNotIn("—", out.split("\n")[-1])

    def test_notes_only_render_when_their_condition_holds(self):
        self._setup(json.dumps({
            "legend": "L.", "state_check_note": " SC.",
            "tool_search_note": " TS.", "tools": {},
        }))
        with patch.object(proxy, "_MCP_STATE_CHECK_TOOLS", set()), \
                patch.object(proxy, "_TOOL_SEARCH_ENABLED", False):
            out = proxy._format_tool_entries([("bash", [], [])])
        self.assertIn("L.", out)
        self.assertNotIn("SC.", out)
        self.assertNotIn("TS.", out)

if __name__ == "__main__":
    unittest.main()
