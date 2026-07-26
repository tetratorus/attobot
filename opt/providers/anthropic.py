"""Anthropic provider — translates OpenAI-shape messages/tools to Anthropic's /v1/messages API and back.

Reads from agent.CFG:
  api_key             required (used as x-api-key)
  model               required (e.g. claude-opus-4-5)
  anthropic_version   defaults to "2023-06-01"
  max_tokens          defaults to 4096
"""
import json
import sys

import requests

import agent

API_URL = "https://api.anthropic.com/v1/messages"


def chat(messages, tools):
    """Translate, call, translate back. Returns an OpenAI-shape assistant message dict.

    Exceptions bubble up to agent.py's retry loop.
    """
    system, anthropic_messages = _split_system_and_convert(messages)
    _mark_message_cache(anthropic_messages)
    body = {
        "model": agent.CFG["model"],
        "max_tokens": int(agent.CFG.get("max_tokens", 4096)),
        "messages": anthropic_messages,
    }
    if system:
        body["system"] = _system_blocks(system)
    if tools:
        body["tools"] = [_convert_tool(t) for t in tools]

    r = requests.post(
        API_URL,
        headers={
            "x-api-key": agent.CFG["api_key"],
            "anthropic-version": agent.CFG.get("anthropic_version", "2023-06-01"),
            "content-type": "application/json",
        },
        json=body,
        timeout=600,  # long reasoning generations exceed 120s; timing out mid-generation = retry forever
    )
    data = r.json()
    if "content" not in data:
        if 400 <= r.status_code < 500 and r.status_code != 429:
            sys.exit(f"fatal llm error {r.status_code}: {data}")  # bad key/model/request — retrying won't help
        raise RuntimeError(f"{r.status_code}: {data}")
    return _convert_response(data)


def _system_blocks(system):
    """Split the system prompt for prompt caching. Caching is a prefix match:
    soul + harness are stable across turns, but <memory> and the <life> tail
    change almost every turn, so the cache breakpoint must sit before them.
    Caches tools + soul + harness (tools render before system)."""
    i = system.find("\n\n<memory>")
    if i == -1:
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    return [
        {"type": "text", "text": system[:i], "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": system[i:]},
    ]


def _mark_message_cache(messages):
    """Incrementally cache the conversation. Anthropic caching is prefix-based: it
    reads the longest previously-written prefix automatically, so we only need a write
    breakpoint near the tail each turn. Without this, the whole growing message history
    (tool results especially) is reprocessed every turn — only the fixed system prefix
    gets cached, which is why cache-read share collapses as the conversation grows.

    Mark the last block of the final two messages so the rolling write breakpoints stay
    within Anthropic's lookback window as history grows. (System holds 1 breakpoint;
    these add up to 2; Anthropic's cap is 4.)"""
    marked = 0
    for m in reversed(messages):
        blocks = m.get("content")
        if not isinstance(blocks, list) or not blocks:
            continue
        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        marked += 1
        if marked == 2:
            break


def _convert_blocks(content):
    """OpenAI-shape content blocks (image_url/text) → Anthropic blocks."""
    blocks = []
    for b in content:
        if b.get("type") == "image_url":
            url = b["image_url"]["url"]
            meta, b64 = url.split(",", 1)
            mime = meta.split(":", 1)[1].split(";", 1)[0]
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": mime, "data": b64}})
        else:
            blocks.append({"type": "text", "text": b.get("text", json.dumps(b))})
    return blocks


def _split_system_and_convert(messages):
    """Peel off role:system into top-level field; convert each remaining message."""
    system = None
    out = []
    for m in messages:
        role = m["role"]
        content = m.get("content")
        if role == "system":
            text = content if isinstance(content, str) else ""
            system = text if system is None else f"{system}\n\n{text}"
            continue
        if role == "assistant":
            blocks = []
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in m.get("tool_calls") or []:
                blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": json.loads(tc["function"]["arguments"] or "{}"),
                })
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            result_content = content if isinstance(content, str) else _convert_blocks(content)
            out.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": m["tool_call_id"],
                "content": result_content,
            }]})
        else:  # user
            if isinstance(content, list):
                out.append({"role": "user", "content": _convert_blocks(content)})
            else:
                out.append({"role": "user", "content": [{"type": "text", "text": content or ""}]})
    return system, out


def _convert_tool(tool):
    fn = tool["function"]
    return {
        "name": fn["name"],
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }


def _convert_response(data):
    """Anthropic response → OpenAI assistant message dict."""
    text_parts = []
    tool_calls = []
    for block in data.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append({
                "id": block["id"],
                "type": "function",
                "function": {
                    "name": block["name"],
                    "arguments": json.dumps(block.get("input") or {}),
                },
            })
    out = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else ""}
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out
