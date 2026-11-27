"""One private media worker per model turn; pipes only, no listening socket."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from .errors import BridgeError

MAX_BYTES = 128 * 1024 * 1024
_ACTIVE_PIDS = set()
_WORKER_SLOTS = threading.BoundedSemaphore(4)


class MediaWorker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.process = None
        self.sequence = 0
        self.resources = False
        self.definitions = []
        self._slot = False

    async def __aenter__(self):
        cfg = self.cfg
        # The workflow constructs these commands; never execute a model-supplied command.
        args = list(cfg.get("args", []))
        scripts = [Path(arg).resolve() for arg in args if str(arg).endswith(".py")]
        allowed = {Path(__file__).with_name(name).resolve() for name in ("media_server.py", "b_media_server.py")}
        if Path(cfg["command"]).resolve() != Path(sys.executable).resolve() or len(scripts) != 1 or scripts[0] not in allowed:
            raise BridgeError("tool_scope", "媒体工具只能使用插件自带 Python 脚本")
        env = {name: value for name, value in os.environ.items()
               if not any(word in name.upper() for word in ("API_KEY", "TOKEN", "PASSWORD")) and name.upper() != "NODE_OPTIONS"}
        for name, value in cfg.get("env", {}).items():
            if any(word in name.upper() for word in ("API_KEY", "TOKEN", "PASSWORD")):
                raise BridgeError("tool_scope", "媒体工具环境不能包含模型凭据")
            env[name] = str(value)
        env.update(KKGO_TOOL_WORKER="1", PYTHONDONTWRITEBYTECODE="1")
        options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {"start_new_session": True}
        while not _WORKER_SLOTS.acquire(blocking=False):
            await asyncio.sleep(.05)
        self._slot = True
        try:
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                cfg["command"], *args, cwd=cfg.get("cwd"), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=MAX_BYTES + 1, **options))
            try:
                self.process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                self.process = await spawn
                raise
            _ACTIVE_PIDS.add(self.process.pid)
            result = await self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "kk-python", "version": "0.2.0"}}, startup=True)
            self.resources = bool((result.get("capabilities") or {}).get("resources") is not None)
            await self.notify("notifications/initialized", {})
            self.definitions = (await self.rpc("tools/list", {}, startup=True)).get("tools", [])
            return self
        except BaseException:
            await self.close()
            raise

    async def notify(self, method, params):
        raw = json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False).encode("utf-8") + b"\n"
        self.process.stdin.write(raw)
        await self.process.stdin.drain()

    async def rpc(self, method, params, *, startup=False):
        self.sequence += 1
        number = self.sequence
        raw = json.dumps({"jsonrpc": "2.0", "id": number, "method": method, "params": params}, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(raw) > MAX_BYTES:
            raise BridgeError("tool_limit", "媒体工具请求超过大小上限")

        async def exchange():
            self.process.stdin.write(raw)
            await self.process.stdin.drain()
            for _ in range(1024):
                line = await self.process.stdout.readline()
                if not line:
                    raise BridgeError("tool_worker", "媒体工具提前退出")
                if len(line) > MAX_BYTES:
                    raise BridgeError("tool_limit", "媒体工具响应超过大小上限")
                message = json.loads(line)
                if message.get("id") != number:
                    continue
                if "error" in message:
                    raise BridgeError("tool_rpc", "媒体工具返回协议错误")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise BridgeError("tool_rpc", "媒体工具返回了无效结果")
                return result
            raise BridgeError("tool_rpc", "媒体工具通知过多，未收到调用结果")

        timeout = self.cfg.get("startupTimeoutMs", 30000) if startup else self.cfg.get("toolCallTimeoutMs", 180000)
        return await asyncio.wait_for(exchange(), max(.1, float(timeout) / 1000))

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        process, self.process = self.process, None
        if process is None:
            if self._slot:
                self._slot = False
                _WORKER_SLOTS.release()
            return
        try:
            if os.name == "nt":
                # Snapshot and kill only descendants of this owned worker.
                import psutil
                try:
                    parent = psutil.Process(process.pid)
                    children = parent.children(recursive=True)
                    for child in reversed(children):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    if process.returncode is None:
                        parent.kill()
                    await asyncio.to_thread(psutil.wait_procs, children, timeout=3)
                except psutil.NoSuchProcess:
                    pass
            else:
                # Encoders inside a worker inherit this session, including grandchildren.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await asyncio.wait_for(process.wait(), 5)
        finally:
            _ACTIVE_PIDS.discard(process.pid)
            if process.stdin is not None:
                process.stdin.close()
            if self._slot:
                self._slot = False
                _WORKER_SLOTS.release()
