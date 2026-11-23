"""Bounded outbound model calls; no server, SDK daemon or global credentials."""
from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import time
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .errors import BridgeError

MAX_BYTES = 128 * 1024 * 1024


def protocol(cfg):
    name = cfg.get("api_protocol", "openai-completions")
    if name == "codez":
        host = urlsplit(cfg["base_url"]).hostname or ""
        return "chat" if host == "zdjapi.com" or host.endswith(".zdjapi.com") else "responses"
    if name == "openai-completions":
        return "chat"
    if name == "deepseek-messages":
        return "messages"
    raise BridgeError("protocol", "不支持的接口协议")


def endpoint(cfg):
    url = urlsplit(cfg["base_url"])
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise BridgeError("provider_url", "Base URL 必须是无账号、查询参数和片段的 HTTP(S) 地址")
    path = url.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/messages"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
    kind = protocol(cfg)
    if kind == "messages" and not path.endswith("/v1"):
        path += "/v1"
    path += {"chat": "/chat/completions", "messages": "/messages", "responses": "/responses"}[kind]
    return urlunsplit((url.scheme, url.netloc, path, "", ""))


def image_block(block):
    """Preserve the original <=1 MiB / JPEG85 transmission policy."""
    data = base64.b64decode(block["data"], validate=True)
    mime = block.get("mimeType", block.get("mime", "image/png"))
    if len(data) > 1024 * 1024 and mime in ("image/png", "image/jpeg"):
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            output = io.BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=85, subsampling=2)
        data, mime = output.getvalue(), "image/jpeg"
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime}


def blocks_for(blocks, kind):
    result = []
    for block in blocks:
        if block["type"] == "text":
            result.append({"type": "input_text" if kind == "responses" else "text", "text": block.get("text", "")})
        elif block["type"] == "image":
            mime, data = block["mimeType"], block["data"]
            uri = "data:" + mime + ";base64," + data
            if kind == "chat":
                result.append({"type": "image_url", "image_url": {"url": uri}})
            elif kind == "messages":
                result.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
            else:
                result.append({"type": "input_image", "image_url": uri})
    return result


def payload(cfg, rules, history, definitions):
    kind = protocol(cfg)
    messages = []
    if kind == "chat":
        role = ("developer" if urlsplit(cfg["base_url"]).hostname == "api.zdjapi.com" and
                cfg["model"] == "gpt-6.1-sol" else "system")
        messages.append({"role": role, "content": rules})
    for message in history:
        role = message["role"]
        if role == "assistant":
            raw = copy.deepcopy(message["raw"])
            if kind == "responses":
                messages.extend(raw)
            else:
                if kind == "chat":
                    for call in raw.get("tool_calls", []):
                        call["function"]["arguments"] = json.dumps(json.loads(call["function"]["arguments"]), ensure_ascii=False, separators=(",", ":"))
                messages.append(raw)
        elif role == "tool":
            content = message["content"]
            if kind == "chat":
                messages.append({"role": "tool", "tool_call_id": message["call_id"],
                                 "content": "\n".join(b["text"] for b in content if b["type"] == "text")})
            elif kind == "responses":
                messages.append({"type": "function_call_output", "call_id": message["call_id"], "output": blocks_for(content, kind)})
            else:
                block = {"type": "tool_result", "tool_use_id": message["call_id"], "content": blocks_for(content, kind), "is_error": message.get("is_error", False)}
                if messages and messages[-1]["role"] == "user":
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
        else:
            messages.append({"role": role, "content": blocks_for(message["content"], kind)})
    body = {"model": cfg["model"], "stream": True}
    tools = []
    for definition in definitions:
        name, description, schema = definition["name"], definition.get("description", ""), definition["inputSchema"]
        if kind == "chat":
            tools.append({"type": "function", "function": {"name": name, "description": description, "parameters": schema}})
        elif kind == "messages":
            tools.append({"name": name, "description": description, "input_schema": schema})
        else:
            tools.append({"type": "function", "name": name, "description": description, "parameters": schema})
    if tools:
        body["tools"] = tools
    if kind == "responses":
        body.update(input=messages, instructions=rules, max_output_tokens=cfg.get("max_tokens", 32768), store=False, include=["reasoning.encrypted_content"])
    else:
        body.update(messages=messages, max_tokens=cfg.get("max_tokens", 32768))
        if kind == "messages":
            body["system"] = rules
        elif cfg.get("stream_usage"):
            body["stream_options"] = {"include_usage": True}
    effort = cfg.get("reasoning_effort", "auto")
    if effort and effort != "auto":
        if kind == "chat":
            body["reasoning_effort"] = effort
        else:
            body["reasoning" if kind == "responses" else "output_config"] = {"effort": effort}
    return body


class Stream:
    """Keep reasoning/signatures and join fragmented tool argument deltas."""
    def __init__(self, kind):
        self.kind = kind
        self.message = {"role": "assistant", "content": ""}
        self.calls, self.blocks, self.partial, self.items = {}, {}, {}, {}
        self.usage = {}
        self.finish = None
        self.complete = None
        self.done = False
        self.first_token = None
        self.started = time.monotonic()

    def consume(self, raw):
        if raw == "[DONE]":
            self.done = True
            return
        event = json.loads(raw)
        if event.get("error"):
            raise BridgeError("provider_stream", "接口返回流式错误，任务未自动重跑")
        if self.kind == "chat":
            self.usage.update(event.get("usage") or {})
            for choice in event.get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta") or {}
                for field in ("content", "reasoning_content", "reasoning", "refusal"):
                    if isinstance(delta.get(field), str):
                        self.message[field] = self.message.get(field, "") + delta[field]
                        if field == "content" and delta[field] and self.first_token is None:
                            self.first_token = time.monotonic() - self.started
                for call in delta.get("tool_calls", []):
                    target = self.calls.setdefault(call.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    target["id"] += call.get("id", "")
                    for field in ("name", "arguments"):
                        target["function"][field] += (call.get("function") or {}).get(field, "")
                self.finish = choice.get("finish_reason") or self.finish
        elif self.kind == "messages":
            name, index = event.get("type"), event.get("index", 0)
            if name == "message_start":
                self.usage.update(event["message"].get("usage") or {})
            elif name == "content_block_start":
                self.blocks[index] = copy.deepcopy(event["content_block"])
            elif name == "content_block_delta":
                block, delta = self.blocks[index], event["delta"]
                field = {"text_delta": "text", "thinking_delta": "thinking", "signature_delta": "signature"}.get(delta["type"])
                if field:
                    block[field] = block.get(field, "") + delta.get(field, "")
                    if field == "text" and delta.get(field) and self.first_token is None:
                        self.first_token = time.monotonic() - self.started
                elif delta["type"] == "input_json_delta":
                    self.partial[index] = self.partial.get(index, "") + delta["partial_json"]
                else:
                    raise BridgeError("provider_stream", "不支持的 Messages 流式数据类型")
            elif name == "message_delta":
                self.finish = event["delta"].get("stop_reason") or self.finish
                self.usage.update(event.get("usage") or {})
            elif name == "message_stop":
                self.done = True
            elif name == "error":
                raise BridgeError("provider_stream", "Messages 流式返回失败")
        else:
            name = event.get("type")
            if name == "response.output_text.delta" and event.get("delta") and self.first_token is None:
                self.first_token = time.monotonic() - self.started
            elif name == "response.output_item.done":
                self.items[event.get("output_index", 0)] = event["item"]
            elif name in ("response.completed", "response.incomplete"):
                self.complete = event["response"]
                self.done = True
            elif name in ("response.failed", "error"):
                raise BridgeError("provider_stream", "Responses 流式返回失败")

    def result(self):
        if not self.done:
            raise BridgeError("provider_stream", "模型流在完成标记前中断，任务未自动重跑")
        if self.kind == "chat":
            if self.calls:
                self.message["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
            return {"choices": [{"message": self.message, "finish_reason": self.finish}], "usage": self.usage}
        if self.kind == "messages":
            for index, raw in self.partial.items():
                self.blocks[index]["input"] = json.loads(raw)
            return {"content": [self.blocks[i] for i in sorted(self.blocks)], "stop_reason": self.finish, "usage": self.usage}
        if self.complete is None:
            raise BridgeError("provider_stream", "Responses 缺少完整结果")
        if not self.complete.get("output") and self.items:
            self.complete["output"] = [self.items[i] for i in sorted(self.items)]
        return self.complete


async def read_response(response, kind):
    stream = Stream(kind)
    if "text/event-stream" not in response.headers.get("Content-Type", ""):
        chunks, total = [], 0
        async for chunk in response.content.iter_chunked(65536):
            total += len(chunk)
            if total > MAX_BYTES:
                raise BridgeError("response_limit", "模型响应超过大小上限")
            chunks.append(chunk)
        complete = json.loads(b"".join(chunks))
    else:
        buffer, data, total = b"", [], 0
        async for chunk in response.content.iter_chunked(65536):
            total += len(chunk)
            if total > MAX_BYTES:
                raise BridgeError("response_limit", "模型响应超过大小上限")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.rstrip(b"\r").decode("utf-8")
                if not line:
                    if data:
                        stream.consume("\n".join(data))
                        data = []
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
        if buffer.strip():
            line = buffer.rstrip(b"\r").decode("utf-8")
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        if data:
            stream.consume("\n".join(data))
        complete = stream.result()
    return normalize(complete, kind), stream.first_token


def normalize(complete, kind):
    if not isinstance(complete, dict) or complete.get("error"):
        raise BridgeError("provider_response", "模型返回无效结果")
    calls = []
    if kind == "chat":
        choices = complete.get("choices") or []
        if not choices:
            raise BridgeError("provider_response", "模型没有返回 choices")
        raw = choices[0]["message"]
        finish = choices[0].get("finish_reason")
        text = raw.get("content") or ""
        for call in raw.get("tool_calls", []):
            calls.append({"id": call["id"], "name": call["function"]["name"], "arguments": json.loads(call["function"]["arguments"])})
    elif kind == "messages":
        raw = {"role": "assistant", "content": complete.get("content", [])}
        finish = complete.get("stop_reason")
        text = "".join(b["text"] for b in raw["content"] if b["type"] == "text")
        calls = [{"id": b["id"], "name": b["name"], "arguments": b["input"]} for b in raw["content"] if b["type"] == "tool_use"]
    else:
        raw = complete.get("output", [])
        finish = complete.get("status")
        text = "".join(b["text"] for item in raw if item.get("type") == "message" for b in item.get("content", []) if b.get("type") == "output_text")
        calls = [{"id": item["call_id"], "name": item["name"], "arguments": json.loads(item["arguments"])} for item in raw if item.get("type") == "function_call"]
    valid = ("tool_calls", "function_call", "tool_use", "stop", "end_turn", "stop_sequence", "completed")
    if finish not in valid:
        raise BridgeError("provider_finish", "模型未正常完成：" + str(finish))
    if calls and finish not in ("tool_calls", "tool_use", "function_call", "completed"):
        raise BridgeError("provider_finish", "工具调用与模型结束原因不一致")
    usage = complete.get("usage") or {}
    input_tokens = usage.get("prompt_tokens" if kind == "chat" else "input_tokens", 0)
    output_tokens = usage.get("completion_tokens" if kind == "chat" else "output_tokens", 0)
    cached = (usage.get("prompt_tokens_details" if kind == "chat" else "input_tokens_details") or {}).get("cached_tokens", 0)
    if kind == "messages":
        cached = usage.get("cache_read_input_tokens", 0)
        input_tokens += cached + usage.get("cache_creation_input_tokens", 0)
    normalized_usage = {"known": bool(usage), "input_tokens": input_tokens, "output_tokens": output_tokens, "cached_tokens": cached,
                        "total_tokens": input_tokens + output_tokens}
    return {"raw": raw, "text": text, "calls": calls, "finish_reason": "tool-calls" if calls else "completed", "usage": normalized_usage}


async def next_turn(client, cfg, rules, history, definitions):
    kind = protocol(cfg)
    raw = json.dumps(payload(cfg, rules, history, definitions), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise BridgeError("request_limit", "模型请求超过128 MiB，未删减素材或自动重跑")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if kind == "messages":
        headers.update({"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    for attempt in range(4):
        started = time.monotonic()
        async with client.post(endpoint(cfg), data=raw, headers=headers, proxy=cfg.get("proxy_url") or None, allow_redirects=False) as response:
            if 200 <= response.status < 300:
                turn, first = await read_response(response, kind)
                turn.update(elapsed=time.monotonic() - started, first_token=first)
                return turn
            status = response.status
            try:
                retry_after = min(30, max(0, float(response.headers.get("Retry-After", 0))))
            except ValueError:
                retry_after = 0
        if attempt < 3 and (status in (408, 429) or 500 <= status < 600):
            await asyncio.sleep(retry_after or 2 ** attempt)
            continue
        raise BridgeError("provider_http", f"模型接口 HTTP {status}，请求体{len(raw)/1048576:.2f} MiB")
