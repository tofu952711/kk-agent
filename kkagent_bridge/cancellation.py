"""Job-owned cancellation shared by Comfy nodes, media workers and AI turns."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import subprocess
import os
import signal
import sys
import threading
import time

_CURRENT = ContextVar("video_cancellation", default=None)
_LOCK = threading.Lock()
_SCOPES = set()


def interrupted():
    module = sys.modules.get("comfy.model_management")
    return bool(module and module.processing_interrupted())


def raise_cancelled():
    # Do not consume Comfy's global flag: parallel nodes must also observe it.
    module = sys.modules.get("comfy.model_management")
    if module is not None:
        raise module.InterruptProcessingException()
    raise RuntimeError("任务已取消")


class CancelScope:
    def __init__(self):
        self.cancelled = threading.Event()
        execution = sys.modules.get("comfy_execution.utils")
        context = execution.get_executing_context() if execution is not None else None
        self.prompt_id = context.prompt_id if context is not None else None
        with _LOCK:
            _SCOPES.add(self)

    def cancel(self):
        self.cancelled.set()

    def check(self):
        if interrupted():
            self.cancel()
        if self.cancelled.is_set():
            raise_cancelled()

    def close(self):
        with _LOCK:
            _SCOPES.discard(self)


def current_scope():
    return _CURRENT.get()


def check_cancelled():
    scope = current_scope()
    if scope is not None:
        scope.check()
    elif interrupted():
        raise_cancelled()


@contextmanager
def bind_scope(scope):
    token = _CURRENT.set(scope)
    try:
        yield
    finally:
        _CURRENT.reset(token)


async def cancellable_to_thread(action, *args, scope=None, **kwargs):
    owned = scope is None
    scope = scope or CancelScope()

    def work():
        with bind_scope(scope):
            scope.check()
            return action(*args, **kwargs)

    # Submit during this coroutine's first scheduling turn. Comfy starts async
    # nodes for one turn before it invokes a synchronous sibling (e.g. Deface).
    # create_task(to_thread(...)) adds a second turn and can leave inference
    # unsubmitted for the entire synchronous sibling's execution.
    context = copy_context()
    task = asyncio.get_running_loop().run_in_executor(None, context.run, work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        scope.cancel()
        # Killing an owned media process/AI turn wakes the worker. Do not close
        # a still-executing generator from the event-loop thread.
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=6)
        except BaseException:
            if not task.done():
                task.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        raise
    finally:
        if owned:
            scope.close()


def install_interrupt_hook(server):
    """Latch a successful native cancel request before another node clears it."""
    from aiohttp import web
    app = getattr(server, "app", None)
    if app is None:
        return

    @web.middleware
    async def cancel_hook(request, handler):
        path = request.path.removeprefix("/api")
        native_cancel = path == "/interrupt" or (
            path.startswith("/jobs/") and path.endswith("/cancel"))
        if request.method != "POST" or not native_cancel:
            return await handler(request)
        target = None
        if path == "/interrupt":
            try:
                body = await request.json()
            except ValueError:
                body = {}
            if isinstance(body, dict):
                target = body.get("prompt_id") or None
        else:
            target = path[len("/jobs/"):-len("/cancel")]
        response = await handler(request)
        if response.status < 400:
            with _LOCK:
                scopes = list(_SCOPES)
            for scope in scopes:
                # A sibling may already have consumed Comfy's global flag.
                if target is None or scope.prompt_id == target or (
                        scope.prompt_id is None and interrupted()):
                    scope.cancel()
        return response

    app.middlewares.append(cancel_hook)


@contextmanager
def watch_bridge(bridge):
    scope = current_scope() or CancelScope()
    owned = current_scope() is None
    stopped = threading.Event()
    bridge._workflow_scope = scope

    def watch():
        while not stopped.wait(.1):
            if interrupted():
                scope.cancel()
            if scope.cancelled.is_set():
                # Repeat through startup races: a process/run may not yet exist.
                bridge.cancel()

    watcher = threading.Thread(target=watch, name="video-cancel", daemon=True)
    watcher.start()
    try:
        scope.check()
        yield
    finally:
        stopped.set()
        watcher.join(timeout=2)
        if owned:
            scope.close()


def kill_process_tree(process):
    # Only signal a process group created by this plugin. The leader may have
    # already exited while a decoder/grandchild still owns our output pipes.
    group = getattr(process, "_kk_owned_pgid", None)
    if sys.platform != "win32" and group is not None:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
        else:
            process.kill()
    process.wait(timeout=5)


def cancellable_run(command, *, timeout=None, capture_output=False, **kwargs):
    """subprocess.run equivalent that can stop even a hung native decoder."""
    check_cancelled()
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    else:
        # All descendants inherit this session/group unless they explicitly detach.
        kwargs.pop("creationflags", None)
        kwargs["start_new_session"] = os.environ.get("KKGO_TOOL_WORKER") != "1"
    process = subprocess.Popen(command, **kwargs)
    if sys.platform != "win32" and kwargs.get("start_new_session"):
        process._kk_owned_pgid = process.pid
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            check_cancelled()
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=.1)
                check_cancelled()
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        kill_process_tree(process)
