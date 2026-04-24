#!/usr/bin/env python3

import os
import requests
import warnings
import json
import re
import datetime
from flask import Flask, request, jsonify

# --- 1. Configuration ---
TARGET_URL = ""
CUSTOM_HEADERS = {
    "Authorization": "",
    "Content-Type": "application/json",
}

OUTPUT_DIR = "output"

app = Flask(__name__)

if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

def save_debug_file(req_id, stage_prefix, stage_name, payload):
    filename = f"{req_id}_{stage_prefix}_{stage_name}.json"
    filepath = os.path.join(OUTPUT_DIR, filename)
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        print(f"[+] Saved debug file: {filename}")
    except Exception as e:
        print(f"[-] Failed to save debug file {filename}: {e}")

def format_tools_to_text(tools_array):
    if not tools_array:
        return "No tools available."
    schema_text = ""
    for tool in tools_array:
        func = tool.get("function", {}) if "function" in tool else tool
        name = func.get("name", "unknown_tool")
        desc = func.get("description", "No description provided.")
        schema_text += f"Tool Name: `{name}`\nDescription: {desc}\nParameters:\n"

        parameters = func.get("parameters", {})
        properties = parameters.get("properties", {})
        # Extract the list of required fields from the JSON schema
        required_fields = parameters.get("required", [])

        if not properties:
            schema_text += "- None\n"
        else:
            for param_name, param_details in properties.items():
                param_type = param_details.get("type", "string")
                param_desc = param_details.get("description", "")

                # Flag the parameter as REQUIRED or Optional in the text prompt
                if param_name in required_fields:
                    req_label = "**REQUIRED**"
                else:
                    req_label = "Optional"

                schema_text += f"- `{param_name}` ({param_type}) [{req_label}]: {param_desc}\n"
        schema_text += "\n"
    return schema_text.strip()

def build_cooperative_prompt_user(original_content, tools_text):
    return f"""You are a helpful and intelligent AI assistant.

### Tool Usage Instructions
You have access to specific tools to help answer the user's request. If you need to use a tool, you MUST output a strictly formatted JSON object inside standard Markdown code blocks (```json ... ```). It must follow this exact structure:
{{
  "name": "<tool_name>",
  "arguments": {{
    <tool_parameters>
  }}
}}
You may explain your thought process before or after the JSON block. If NO tools are needed, simply answer the user normally.

### Available Tools
{tools_text}

### User Request
"{original_content}"
"""

def build_cooperative_prompt_tool(original_content, tools_text):
    return f"""You are a helpful AI assistant executing a multi-step process.

### Tool Usage Instructions
Review the latest System Observation below. If you need to use another tool to continue, output a strictly formatted JSON object inside standard Markdown code blocks (```json ... ```) with this structure:
{{
  "name": "<tool_name>",
  "arguments": {{
    <tool_parameters>
  }}
}}
You may explain your reasoning before or after the JSON block. If the task is fully complete, answer the user normally without any JSON.

### Available Tools
{tools_text}

### Latest System Observation
"{original_content}"
"""

def translate_history_and_apply_prompt(original_messages, tools_text):
    if not original_messages:
        return []

    messages = []
    tool_call_map = {}

    for msg in original_messages:
        role = msg.get("role")
        content = msg.get("content", "") or ""

        if role == "system":
            messages.append({"role": "system", "content": content})

        elif role == "user":
            if messages and messages[-1]["role"] == "user":
                messages[-1]["content"] += f"\n\n{content}"
            else:
                messages.append({"role": "user", "content": content})

        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    tc_id = tc.get("id", "unknown_id")
                    func = tc.get("function", {})
                    name = func.get("name", "unknown")
                    args = func.get("arguments", "{}")

                    tool_call_map[tc_id] = name

                    md_block = f"```json\n{{\n  \"name\": \"{name}\",\n  \"arguments\": {args}\n}}\n```"
                    content += f"\n{md_block}\n"
            messages.append({"role": "assistant", "content": content.strip()})

        elif role == "tool":
            tc_id = msg.get("tool_call_id", "unknown_id")
            tool_name = msg.get("name", tool_call_map.get(tc_id, "unknown_tool"))

            observation = f"[System Observation: Tool '{tool_name}' executed. Result:]\n{content}"

            if messages and messages[-1]["role"] == "user":
                messages[-1]["content"] += f"\n\n{observation}"
            else:
                messages.append({"role": "user", "content": observation})

    if tools_text and messages and messages[-1]["role"] == "user":
        original_last_role = original_messages[-1].get("role")
        final_content = messages[-1]["content"]

        if original_last_role == "tool":
            messages[-1]["content"] = build_cooperative_prompt_tool(final_content, tools_text)
        else:
            messages[-1]["content"] = build_cooperative_prompt_user(final_content, tools_text)

    return messages

def extract_tool_call_and_text(response_text):
    """ Extracts JSON tool payload and cleanly removes it from the conversational text. """
    match = re.search(r'```json\s*(.*?)\s*```', response_text, re.DOTALL)
    payload = None
    clean_text = response_text

    if match:
        raw_json_string = match.group(1).strip()
        try:
            payload = json.loads(raw_json_string)
            if "name" in payload and "arguments" in payload:
                # Remove the JSON markdown block so the user only sees the text
                clean_text = response_text.replace(match.group(0), "").strip()
        except json.JSONDecodeError:
            payload = None

    return payload, clean_text

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
def catch_all(path):
    warnings.filterwarnings('ignore', message='Unverified HTTPS request')
    req_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    try:
        client_json = json.loads(request.get_data())
        save_debug_file(req_id, "01", "Goose_Request", client_json)

        messages_from_client = client_json.get("messages", [])
        tools = client_json.get("tools", [])
        tools_text = format_tools_to_text(tools) if tools else ""

        messages_for_api = translate_history_and_apply_prompt(messages_from_client, tools_text)

        final_payload = {
            "model": "",
            "messages": messages_for_api
        }

        save_debug_file(req_id, "02", "API_Request", final_payload)

        proxied_response = requests.request(
            method=request.method,
            url=TARGET_URL,
            headers=CUSTOM_HEADERS,
            json=final_payload,
            verify=False,
            timeout=180
        )

        if not proxied_response.ok:
            error_data = {"status_code": proxied_response.status_code, "text": proxied_response.text}
            save_debug_file(req_id, "03", "API_Error", error_data)
            return jsonify({"error": "Target server error", "details": proxied_response.text}), proxied_response.status_code

        target_json = proxied_response.json()
        save_debug_file(req_id, "03", "API_Response", target_json)

        try:
            raw_content = target_json["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raw_content = str(target_json)

        # Parse out both the tool call and the conversational text
        tool_call_payload, clean_text = extract_tool_call_and_text(raw_content)

        is_stream = client_json.get("stream", False)

        if is_stream:
            def generate_sse():
                # 1. Initial chunk
                yield f"data: {json.dumps({'id': req_id, 'object': 'chat.completion.chunk', 'model': 'icarus:latest', 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"

                # 2. Content chunk (Yields the model's text explanation before the tool executes)
                if clean_text:
                    yield f"data: {json.dumps({'id': req_id, 'object': 'chat.completion.chunk', 'model': 'icarus:latest', 'choices': [{'index': 0, 'delta': {'content': clean_text}, 'finish_reason': None}]})}\n\n"

                # 3. Tool chunk
                if tool_call_payload:
                    tc_delta = {
                        "tool_calls": [{
                            "index": 0,
                            "id": f"call_{req_id}",
                            "type": "function",
                            "function": {
                                "name": tool_call_payload["name"],
                                "arguments": json.dumps(tool_call_payload["arguments"])
                            }
                        }]
                    }
                    yield f"data: {json.dumps({'id': req_id, 'object': 'chat.completion.chunk', 'model': 'icarus:latest', 'choices': [{'index': 0, 'delta': tc_delta, 'finish_reason': None}]})}\n\n"

                # 4. Finish chunk
                finish_reason = "tool_calls" if tool_call_payload else "stop"
                yield f"data: {json.dumps({'id': req_id, 'object': 'chat.completion.chunk', 'model': 'icarus:latest', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish_reason}]})}\n\n"

                # 5. Done marker
                yield "data: [DONE]\n\n"

            return app.response_class(generate_sse(), mimetype='text/event-stream')

        else:
            # Non-streaming fallback
            message_obj = {"role": "assistant", "content": clean_text if clean_text else None}

            if tool_call_payload:
                message_obj["tool_calls"] = [{
                    "id": f"call_{req_id}",
                    "type": "function",
                    "function": {
                        "name": tool_call_payload["name"],
                        "arguments": json.dumps(tool_call_payload["arguments"])
                    }
                }]

            return jsonify({
                "id": req_id,
                "object": "chat.completion",
                "created": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
                "model": "icarus:latest",
                "choices": [{
                    "index": 0,
                    "message": message_obj,
                    "finish_reason": "tool_calls" if tool_call_payload else "stop"
                }]
            })

    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        error_payload = {"proxy_error": str(e)}
        save_debug_file(req_id, "99", "Fatal_Error", error_payload)
        return jsonify(error_payload), 500

def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 8080))
    print(f"========================================")
    print(f"🚀 State-Aware Agent Proxy Running (v13)")
    print(f"[*] Listening on: http://{host}:{port}")
    print(f"[*] Dumping JSON payloads to: ./{OUTPUT_DIR}/")
    print(f"========================================")
    app.run(host=host, port=port, debug=False)

if __name__ == "__main__":
    main()
