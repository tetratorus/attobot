import requests

ENDPOINT = "responses"


def _content(content, inbound=True):
    if isinstance(content, str):
        return content
    result = []
    for block in content or []:
        if inbound and block["type"] == "image_url":
            result.append({"type": "input_image", "image_url": block["image_url"]["url"]})
        elif not inbound and block["type"] == "input_image":
            result.append({"type": "image_url", "image_url": {"url": block["image_url"]}})
        else:
            result.append({"type": "input_text" if inbound else "text", "text": block.get("text", "")})
    return result


def build_request(messages, tools, config):
    instructions, inputs = [], []
    for message in messages:
        role, content = message["role"], message.get("content") or ""
        if role == "system":
            instructions.append(content)
        elif role == "tool":
            inputs.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": _content(content)})
        elif role == "assistant":
            state = message.get("provider_state", {}).get("openai_responses")
            if state:
                inputs.extend(state)
                continue
            if content:
                inputs.append({"role": role, "content": content})
            for call in message.get("tool_calls") or []:
                inputs.append({"type": "function_call", "call_id": call["id"],
                               "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
        else:
            inputs.append({"role": role, "content": _content(content)})
    body = {"model": config["model"], "instructions": "\n\n".join(instructions), "input": inputs,
            "store": False, "include": ["reasoning.encrypted_content"],
            "max_output_tokens": int(config.get("max_tokens", 8192))}
    if effort := config.get("reasoning_effort"):
        body["reasoning"] = {"effort": effort}
    if tools:
        body["tools"] = [{"type": "function", **tool["function"], "strict": False} for tool in tools]
    return body


def parse_response(data):
    if data.get("status") != "completed":
        raise ValueError(f"Responses status {data.get('status')}: {data.get('incomplete_details') or data.get('error')}")
    output = data.get("output", [])
    text, calls = [], []
    for item in output:
        if item.get("type") == "message" or item.get("role") == "assistant":
            content = item.get("content", [])
            text.extend([content] if isinstance(content, str) else [part.get("text", part.get("refusal", "")) for part in content])
        elif item["type"] == "function_call":
            calls.append({"id": item["call_id"], "type": "function",
                          "function": {"name": item["name"], "arguments": item["arguments"]}})
    result = {"role": "assistant", "content": "\n".join(text), "provider_state": {"openai_responses": output}}
    if calls:
        result["tool_calls"] = calls
    return result


def normalize_request(body):
    messages, pending = [], []
    if body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})
    def flush():
        if pending:
            messages.append(parse_response({"status": "completed", "output": list(pending)}))
            pending.clear()
    for item in body["input"]:
        if item.get("type") in ("reasoning", "function_call") or item.get("role") == "assistant":
            pending.append(item)
            continue
        flush()
        if item.get("type") == "function_call_output":
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": _content(item["output"], False)})
        else:
            messages.append({"role": item["role"], "content": _content(item.get("content", ""), False)})
    flush()
    return {"messages": messages, "tools": [{"type": "function", "function": {key: value for key, value in tool.items() if key != "type"}} for tool in body.get("tools", [])]}


def chat(messages, tools):
    import agent
    response = requests.post(f"{agent.CFG['api_base'].rstrip('/')}/{ENDPOINT}",
                             headers={"Authorization": f"Bearer {agent.CFG['api_key']}"},
                             json=build_request(messages, tools, agent.CFG), timeout=600)
    data = agent.llm_response(response)
    try:
        return parse_response(data)
    except ValueError as error:
        raise agent.FatalLLMError(400, str(error)) from error
