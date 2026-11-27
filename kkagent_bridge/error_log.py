"""One task's bounded, local request evidence. A new task removes the old error."""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
import uuid
from urllib.parse import urlsplit

from . import paths

_LOCK = threading.RLock()
_BOOT = uuid.uuid4().hex
_CURRENT = None
MAX_REQUESTS = 32
MAX_PREVIEW = 2048


def log_file():
    return paths.bridge_data_dir() / "latest-error.json"


def redact(value, secrets=()):
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[密钥已隐藏]")
            for length in range(min(len(secret) - 1, len(text)), 7, -1):
                if text.endswith(secret[:length]):
                    text = text[:-length] + "[密钥已隐藏]"
                    break
    text = re.sub(r"sk-[\w-]+|Bearer\s+[^\s\"']+", "[密钥已隐藏]", text, flags=re.I)
    text = re.sub(r"data:[^\s\"']*;base64,[A-Za-z0-9+/=]+", "[图片数据已省略]", text)
    return text


def _save():
    try:
        target = log_file()
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(_CURRENT, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(target)
    except OSError as exc:
        logging.getLogger(__name__).warning("KK错误日志写入失败：%s", type(exc).__name__)


def begin(run_id, node_id):
    global _CURRENT
    with _LOCK:
        for target in (log_file(), log_file().with_suffix(".tmp")):
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                logging.getLogger(__name__).warning("KK旧错误日志清理失败：%s", type(exc).__name__)
        _CURRENT = {"run_id": run_id, "node_id": node_id, "started_at": time.time(),
                    "status": "running", "process": _BOOT, "requests": [], "omitted_requests": 0}


def update(run_id, data):
    with _LOCK:
        if _CURRENT is None or _CURRENT["run_id"] != run_id or _CURRENT["status"] != "running":
            return
        row = copy.deepcopy(data["transport"])
        row.update(phase=data.get("phase"), role=data.get("b_agent_role"), segment_index=data.get("segment_index"))
        requests = _CURRENT["requests"]
        previous = next((i for i, item in enumerate(requests) if item["request_id"] == row["request_id"]), None)
        if previous is None:
            if len(requests) == MAX_REQUESTS:
                requests.pop(0)
                _CURRENT["omitted_requests"] += 1
            requests.append(row)
        else:
            requests[previous] = row
        _CURRENT["updated_at"] = time.time()
        _save()


def finish(run_id, status, error=None):
    with _LOCK:
        if _CURRENT is None or _CURRENT["run_id"] != run_id:
            return
        _CURRENT.update(status=status, finished_at=time.time())
        if status == "completed":
            try:
                log_file().unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning("KK成功任务日志清理失败")
        else:
            if error is not None:
                _CURRENT["error"] = {"type": type(error).__name__, "message": redact(error)[:1000]}
            _save()
            print(f"[KK错误日志] 节点{_CURRENT['node_id']}，本次{status}；详细记录：{log_file()}", flush=True)


def latest():
    with _LOCK:
        record = copy.deepcopy(_CURRENT)
        if record is None:
            try:
                record = json.loads(log_file().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"error": None, "running": False}
        if record["status"] == "running" and record.get("process") == _BOOT:
            return {"error": None, "running": True}
        if record["status"] == "completed":
            return {"error": None, "running": False}
        if record["status"] == "running":
            record["status"] = "interrupted"
            record["error"] = {"type": "ProcessInterrupted", "message": "进程已重启；以下为中断前最后记录，未观察到任务结束。"}
        record.pop("process", None)
        return {"error": record, "running": False}


def clear():
    global _CURRENT
    with _LOCK:
        _CURRENT = None
        log_file().unlink(missing_ok=True)
        log_file().with_suffix(".tmp").unlink(missing_ok=True)


class RequestTrace:
    def __init__(self, callback, request_id, cfg):
        self.callback = callback
        self.secrets = tuple(str(value) for key, value in cfg.items()
                             if re.search(r"key|password|secret|token", key, re.I) and isinstance(value, str) and value)
        self.started = time.monotonic()
        self.published = 0
        self.raw_preview = ""
        self.text_preview = ""
        self.row = {"request_id": request_id, "model": cfg.get("model"),
                    "host": urlsplit(cfg.get("base_url", "")).hostname, "started_at": time.time(),
                    "state": "preparing_request", "http_status": None, "response_headers_received": False,
                    "response_bytes": 0, "response_chunks": 0, "first_byte_seconds": None,
                    "first_content_seconds": None, "content_characters": 0, "reasoning_characters": 0,
                    "done_received": False, "eof_received": False, "attempt": 0}

    def publish(self, force=False):
        now = time.monotonic()
        if not force and now - self.published < 1:
            return
        self.published = now
        snapshot = {**self.row, "elapsed_seconds": round(now - self.started, 3),
                    "response_preview": redact(self.raw_preview, self.secrets)[:MAX_PREVIEW],
                    "content_preview": redact(self.text_preview, self.secrets)[:MAX_PREVIEW]}
        self.callback({"kind": "transport", "request_id": self.row["request_id"], "transport": snapshot})

    def sending(self, size, attempt):
        self.row.update(state="waiting_response_headers", request_bytes=size, attempt=attempt)
        self.publish(True)

    def headers(self, response):
        self.row.update(state="receiving_response", response_headers_received=True, http_status=response.status,
                        content_type=response.headers.get("Content-Type", ""), headers_seconds=round(time.monotonic() - self.started, 3))
        self.publish(True)

    def chunk(self, chunk):
        first = self.row["response_chunks"] == 0
        if first:
            self.row["first_byte_seconds"] = round(time.monotonic() - self.started, 3)
        self.row["response_bytes"] += len(chunk)
        self.row["response_chunks"] += 1
        if len(self.raw_preview) < MAX_PREVIEW * 2:
            self.raw_preview += chunk.decode("utf-8", errors="replace")[:MAX_PREVIEW * 2 - len(self.raw_preview)]
        self.publish(first)

    def event(self, raw, kind):
        if raw == "[DONE]":
            self.row.update(done_received=True, state="completion_marker_received")
            self.publish(True)
            return
        event = json.loads(raw)
        texts, reasoning = [], []
        if kind == "chat":
            for choice in event.get("choices", []):
                delta = choice.get("delta") or {}
                texts.append(delta.get("content") or "")
                reasoning.append(delta.get("reasoning_content") or delta.get("reasoning") or "")
                if choice.get("finish_reason"):
                    self.row["finish_reason"] = choice["finish_reason"]
        elif kind == "messages":
            delta = event.get("delta") or {}
            block = event.get("content_block") or {}
            texts.append(delta.get("text") or block.get("text") or "")
            reasoning.append(delta.get("thinking") or block.get("thinking") or "")
        elif event.get("type") == "response.output_text.delta":
            texts.append(event.get("delta") or "")
        text = "".join(value for value in texts if isinstance(value, str))
        first = bool(text) and self.row["first_content_seconds"] is None
        if first:
            self.row["first_content_seconds"] = round(time.monotonic() - self.started, 3)
        self.row["content_characters"] += len(text)
        self.row["reasoning_characters"] += sum(len(value) for value in reasoning if isinstance(value, str))
        self.text_preview += text[:max(0, MAX_PREVIEW * 2 - len(self.text_preview))]
        done = event.get("type") in ("message_stop", "response.completed", "response.incomplete")
        if done:
            self.row.update(done_received=True, state="completion_marker_received")
        self.publish(first or done)

    def eof(self):
        self.row["eof_received"] = True
        self.publish(True)

    def completed(self, text):
        if text and not self.row["content_characters"]:
            self.row.update(content_characters=len(text), first_content_seconds=round(time.monotonic() - self.started, 3))
            self.text_preview = text[:MAX_PREVIEW * 2]
        self.row["state"] = "completed"
        self.publish(True)

    def summary(self):
        row = self.row
        observed = "收到结束标记" if row["done_received"] else ("收到正文" if row["content_characters"] else ("收到响应数据" if row["response_bytes"] else ("仅收到响应头" if row["response_headers_received"] else "未观察到响应头")))
        preview = redact(self.text_preview or self.raw_preview, self.secrets)[:240].replace("\n", "\\n")
        return (f"；实际状态={observed}；HTTP={row['http_status'] if row['http_status'] is not None else '未记录'}"
                f"；响应={row['response_bytes']}字节；正文={row['content_characters']}字符"
                f"；结束标记={'是' if row['done_received'] else '未观察到'}"
                f"；响应结束={'是' if row['eof_received'] else '未观察到'}"
                + (f"；响应片段={preview}" if preview else ""))

    def failed(self, error):
        self.row.update(state="cancelled" if type(error).__name__ == "CancelledError" else "error",
                        exception_type=type(error).__name__)
        self.publish(True)
