"""Task-local Python agent. No Go, background HTTP service or shared sessions."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import threading
import time
import uuid
import weakref

import aiohttp
from jsonschema import Draft202012Validator

from .errors import BridgeError
from .error_log import RequestTrace
from .python_mcp import MediaWorker, _ACTIVE_PIDS
from .python_provider import image_block, next_turn, protocol

_RUNTIMES = weakref.WeakSet()
_STATE_LOCK = threading.RLock()


def sdk_info():
    return {"installed": True, "compatible": True, "backend": "python", "version": "0.2.0", "bundled": False}


def runtime_status():
    with _STATE_LOCK:
        return {"backend": "python", "persistent_service": False,
                "active_turns": sum(bool(runtime.active_session) for runtime in _RUNTIMES),
                "media_workers": len(_ACTIVE_PIDS)}


def close_all_runtimes():
    with _STATE_LOCK:
        runtimes = list(_RUNTIMES)
    for runtime in runtimes:
        runtime.stop()


def _definitions(worker, kind, cfg):
    definitions = copy.deepcopy(worker.definitions)
    args = list(cfg.get("args", []))
    phase = "reverse"
    try:
        output = Path(args[args.index("--output") + 1])
        policy = output / "duration_policy.json"
        if policy.is_file():
            phase = json.loads(policy.read_text(encoding="utf-8")).get("phase", "reverse")
    except (ValueError, IndexError):
        pass
    if any(str(arg).endswith("b_media_server.py") for arg in args):
        phase = "reverse"
    if phase != "segment":
        definitions = [d for d in definitions if d["name"] != "set_video_segments"]
    if kind == "chat":
        for definition in definitions:
            definition["name"] = "mcp__video__" + definition["name"]
        for name in ("list_mcp_resource_templates", "list_mcp_resources", "read_mcp_resource"):
            properties = {"server": {"type": "string"}}
            required = ["server"]
            properties["uri" if name == "read_mcp_resource" else "cursor"] = {"type": "string"}
            if name == "read_mcp_resource":
                required.append("uri")
            definitions.append({"name": name, "description": "Read or list resources on the configured video MCP server.",
                                "inputSchema": {"type": "object", "properties": properties, "required": required}})
        definitions.sort(key=lambda d: d["name"])
    return definitions


def _chat_tool_content(content, workspace):
    """Keep the existing chat continuation's text-only attachment representation."""
    from PIL import Image
    parts = []
    for block in content:
        if block["type"] == "text":
            parts.append(block["text"])
        elif block["type"] == "image":
            raw = base64.b64decode(block["data"], validate=True)
            digest = hashlib.sha256(raw).hexdigest()
            target = Path(workspace) / "attachments" / digest
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                mime = Image.MIME.get(image.format, block.get("mimeType", "image/jpeg"))
            extension = ".jpg" if mime == "image/jpeg" else ".png"
            parts.append(f'Image sha256:{digest}; request preview {width}x{height}px. Normalized copy (read-only; may be resized or re-encoded): {json.dumps(str(target))} ({width}x{height}px, {mime}). Source dimensions, format, and byte size may differ. Copy to a writable path ending in {extension} before editing.')
    return [{"type": "text", "text": "\n".join(parts)}]


class SDKRuntime:
    """Compatibility interface used by the existing Python business code."""
    external = False

    def __init__(self, cfg):
        self.cfg = cfg
        self._turn_lock = threading.Lock()
        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._alive = False
        self._active = ""
        self._loop = self._task = None
        self._sessions = {}
        self.last_diagnostics = {}
        self._last_request_trace = None
        with _STATE_LOCK:
            _RUNTIMES.add(self)

    @property
    def alive(self):
        return self._alive

    @property
    def pid(self):
        return None

    @property
    def active_session(self):
        return self._active

    def start(self):
        self._alive = True

    def cancel(self):
        with self._lock:
            already_cancelled = self._cancelled.is_set()
            self._cancelled.set()
            if not already_cancelled and self._loop is not None and self._task is not None and not self._task.done():
                self._loop.call_soon_threadsafe(self._task.cancel)
            return bool(self._active)

    def stop(self):
        self.cancel()
        self._alive = False
        if not self._active:
            self._sessions.clear()

    def run(self, session, blocks, timeout, on_progress=None):
        if not self._turn_lock.acquire(blocking=False):
            raise BridgeError("busy", "当前会话已有任务运行")
        with self._lock:
            self._cancelled.clear()
            self._active = session
            self._alive = True
            self._last_request_trace = None
        try:
            async def execute():
                with self._lock:
                    self._loop = asyncio.get_running_loop()
                    self._task = asyncio.current_task()
                    if self._cancelled.is_set():
                        raise asyncio.CancelledError()
                return await asyncio.wait_for(self._run(session, blocks, on_progress), timeout=float(timeout))
            return asyncio.run(execute())
        except asyncio.CancelledError:
            raise BridgeError("cancelled", "任务已取消，模型连接和媒体工具已关闭" + self._request_summary()) from None
        except asyncio.TimeoutError:
            raise BridgeError("timeout", "任务超时，模型连接和媒体工具已关闭" + self._request_summary()) from None
        except aiohttp.ClientError:
            raise BridgeError("provider_transport", "模型连接失败或中断，未自动重跑" + self._request_summary()) from None
        except BridgeError as exc:
            raise BridgeError(exc.code, str(exc).removeprefix(exc.code + ": ") + self._request_summary()) from None
        finally:
            with self._lock:
                self._loop = self._task = None
                self._active = ""
                if not self._alive:
                    self._sessions.clear()
            self._turn_lock.release()

    def _request_summary(self):
        return self._last_request_trace.summary() if self._last_request_trace is not None else ""

    async def _run(self, session, blocks, progress):
        cfg = self.cfg
        rules = cfg.get("video_system_prompt", "")
        if not isinstance(rules, str) or not rules.strip():
            raise BridgeError("system_rules", "请填写本阶段系统提示词")
        if not cfg.get("api_key"):
            raise BridgeError("api_key", "请在本节点填写模型 API Key")
        kind = protocol(cfg)
        history = copy.deepcopy(self._sessions.get(session, []))
        task = cfg["video_user_prompt"] if "video_user_prompt" in cfg else "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        content = [{"type": "text", "text": task}]
        if cfg.get("video_media_evidence"):
            content.append({"type": "text", "text": cfg["video_media_evidence"]})
        content.extend(image_block(block) for block in blocks if block.get("type") == "image")
        history.append({"role": "user", "content": content})
        events, records = [], []
        totals = {"known": True, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "total_tokens": 0}

        def emit(name, data):
            events.append({"seq": len(events) + 1, "type": name, "data": data})
            if progress:
                progress(name)

        def monitor(record):
            callback = cfg.get("_token_monitor")
            if callback:
                callback.handle(record)

        async with aiohttp.ClientSession(trust_env=not cfg.get("strip_proxy_env", True), timeout=aiohttp.ClientTimeout(total=None, connect=30), read_bufsize=65536) as client:
            worker = MediaWorker(cfg["media_mcp"]) if cfg.get("media_mcp") else None
            try:
                definitions = []
                if worker:
                    await worker.__aenter__()
                    definitions = _definitions(worker, kind, cfg["media_mcp"])
                validators = {d["name"]: Draft202012Validator(d["inputSchema"]) for d in definitions}
                completed, tool_count = {}, 0
                for round_index in range(int(cfg.get("max_agent_rounds", 20))):
                    retry_limit = int(cfg.get("_empty_response_retries", 0))
                    for empty_attempt in range(retry_limit + 1):
                        request_id = uuid.uuid4().hex
                        emit("step/start", {"round": round_index + 1, "request_id": request_id})
                        base = {"request_id": request_id, "model": cfg["model"], "elapsed_seconds": 0, "first_token_seconds": None,
                                "output_tokens": None, "average_tps": None, "finish_reason": "", "usage": {}}
                        monitor({**base, "kind": "start"})
                        began = time.monotonic()
                        trace = RequestTrace(monitor, request_id, cfg)
                        self._last_request_trace = trace
                        try:
                            turn = await next_turn(client, cfg, rules, history, definitions, trace=trace)
                        except BaseException as exc:
                            trace.failed(exc)
                            reason = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                            monitor({**base, "kind": "end", "elapsed_seconds": time.monotonic() - began, "finish_reason": reason})
                            raise
                        usage, elapsed = turn["usage"], turn["elapsed"]
                        empty = not turn["calls"] and not turn["text"].strip()
                        record = {**base, "kind": "end", "elapsed_seconds": elapsed, "first_token_seconds": turn["first_token"],
                                  "output_tokens": usage["output_tokens"] if usage["known"] else None,
                                  "average_tps": usage["output_tokens"] / elapsed if usage["known"] and elapsed else None,
                                  "finish_reason": "empty_response" if empty else turn["finish_reason"], "usage": usage}
                        monitor(record)
                        records.append(record)
                        totals["known"] = totals["known"] and usage["known"]
                        for field in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
                            totals[field] += usage[field]
                        emit("assistant/message", {"usage": usage, "tool_count": len(turn["calls"]), "text_characters": len(turn["text"])})
                        if not empty:
                            break
                        if empty_attempt == retry_limit:
                            message = f"模型返回空白正文，已额外重试{retry_limit}次仍失败" if retry_limit else "模型返回空白正文，未自动重跑"
                            raise BridgeError("empty_response", message)
                        # Resend the same request; do not append the empty reply or repeat completed tools.
                        print(f"[KKGO] 子智能体返回空白正文，保持原规则和素材重试 {empty_attempt + 1}/{retry_limit}", flush=True)
                        emit("step/retry", {"reason": "empty_response", "attempt": empty_attempt + 1, "limit": retry_limit})
                        await asyncio.sleep(2 ** empty_attempt)
                    history.append({"role": "assistant", "raw": turn["raw"]})
                    if not turn["calls"]:
                        self._sessions[session] = history
                        emit("turn/end", {"finish_reason": "completed"})
                        self.last_diagnostics = {"backend": "python", "finish_reason": "completed", "token_speed": records}
                        return {"text": turn["text"], "usage": totals, "events": events, "finish_reason": "completed", "token_speed": records, "kind": "prompt"}
                    if len(turn["calls"]) + tool_count > int(cfg.get("max_agent_tool_calls", 40)):
                        raise BridgeError("tool_limit", "模型工具调用次数达到上限")
                    for call in turn["calls"]:
                        tool_count += 1
                        name, arguments, number = call["name"], call["arguments"], call["id"]
                        if not number or name not in validators:
                            raise BridgeError("tool_scope", "模型调用了当前阶段未开放的工具")
                        signature = json.dumps([name, arguments], ensure_ascii=False, sort_keys=True)
                        if number in completed:
                            if completed[number][0] != signature:
                                raise BridgeError("tool_id", "模型复用了不同参数的工具调用 ID")
                            history.append(copy.deepcopy(completed[number][1]))
                            continue
                        emit("tool/call", {"id": number, "name": name, "arguments": arguments})
                        validation_errors = sorted(validators[name].iter_errors(arguments), key=lambda error: str(error.path))
                        if validation_errors:
                            result = {"isError": True, "content": [{"type": "text", "text": "Invalid tool arguments: " + validation_errors[0].message[:1000]}]}
                        elif name in ("list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"):
                            result = await self._resource(worker, name, arguments)
                        else:
                            local_name = name.removeprefix("mcp__video__") if kind == "chat" else name
                            result = await worker.rpc("tools/call", {"name": local_name, "arguments": arguments})
                        output = result.get("content", [])
                        if kind == "chat":
                            output = _chat_tool_content(output, cfg["workspace"])
                        else:
                            output = [image_block(b) if b["type"] == "image" else b for b in output if b["type"] in ("text", "image")]
                        message = {"role": "tool", "call_id": number, "content": output, "is_error": bool(result.get("isError"))}
                        history.append(message)
                        completed[number] = signature, message
                        emit("tool/result", {"id": number, "name": name, "is_error": message["is_error"], "content_blocks": len(output)})
                    self._sessions[session] = copy.deepcopy(history)
                raise BridgeError("round_limit", "模型执行轮次达到上限")
            finally:
                if worker:
                    await worker.close()

    async def _resource(self, worker, name, arguments):
        if arguments.get("server") != "video":
            return {"isError": True, "content": [{"type": "text", "text": "Unknown MCP server"}]}
        key = {"list_mcp_resources": "resources", "list_mcp_resource_templates": "resourceTemplates", "read_mcp_resource": "contents"}[name]
        if not worker.resources:
            if name == "read_mcp_resource":
                return {"isError": True, "content": [{"type": "text", "text": "MCP resources are not available on this video server"}]}
            result = {key: []}
        else:
            method = {"list_mcp_resources": "resources/list", "list_mcp_resource_templates": "resources/templates/list", "read_mcp_resource": "resources/read"}[name]
            result = await worker.rpc(method, {k: v for k, v in arguments.items() if k != "server"})
        return {"content": [{"type": "text", "text": "MCP server: video\n" + json.dumps(result, ensure_ascii=False)}]}


class SDKChannel:
    def __init__(self, name, cfg, proc):
        self.name, self.cfg, self.proc = name, cfg, proc
        self._connected = False

    def connect(self):
        self.proc.start()
        self._connected = True

    @property
    def ready(self):
        return self._connected and self.proc.alive

    def ask(self, session_id, blocks, timeout=None, on_progress=None):
        self._connected = True
        return self.proc.run(session_id, blocks, float(timeout or self.cfg.get("request_timeout_s", 300)), on_progress)

    def close(self):
        self._connected = False
