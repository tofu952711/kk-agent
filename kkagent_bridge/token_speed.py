"""Provider usage and request timings retained for local diagnostics."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from . import paths, error_log

_LOCK = threading.Lock()
_ACTIVE = {}


def speed_dir():
    return paths.ensure_dir(paths.bridge_data_dir() / 'token-speed')


def latest_file(node_id):
    key = hashlib.sha256(str(node_id).encode()).hexdigest()[:20]
    return speed_dir() / f'node-{key}.json'


def latest_snapshot(node_id):
    with _LOCK:
        monitor = _ACTIVE.get(str(node_id))
    if monitor:
        with monitor.lock:
            return monitor.snapshot()
    try:
        payload = json.loads(latest_file(node_id).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if payload.get('status') == 'running':
        payload['status'] = 'interrupted'
        for row in payload.get('requests', []):
            if row['kind'] != 'end':
                row.update(kind='end', finish_reason='interrupted', output_tokens=None, average_tps=None)
    return payload


class SpeedRun:
    def __init__(self, node_id, model):
        self.node_id = str(node_id) if node_id is not None else None
        self.run_id = uuid.uuid4().hex
        self.model = model
        self.phase = 'reverse'
        self.started = time.monotonic()
        self.requests = {}
        self.received_times = {}
        self.status = 'running'
        self.lock = threading.RLock()

    def __enter__(self):
        error_log.begin(self.run_id, self.node_id)
        with _LOCK:
            _ACTIVE[str(self.node_id)] = self
        self.publish(save=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        from .cancellation import current_scope
        scope = current_scope()
        cancelled = ((scope is not None and scope.cancelled.is_set()) or
                     (exc_type is not None and exc_type.__name__ in ('InterruptProcessingException', 'CancelledError')))
        status = 'cancelled' if cancelled else ('error' if exc_type else 'completed')
        self.finish(status)
        error_log.finish(self.run_id, status, exc)

    def snapshot(self):
        rows = [{**row, 'elapsed_seconds': row['elapsed_seconds'] +
                 (time.monotonic() - self.received_times[key] if row['kind'] != 'end' else 0)}
                for key, row in self.requests.items()]
        ended = [r for r in rows if r['kind'] == 'end']
        known = [r for r in ended if r.get('output_tokens') is not None]
        tokens = sum(r['output_tokens'] for r in known)
        seconds = sum(r['elapsed_seconds'] for r in known)
        elapsed = time.monotonic() - self.started
        return {'node_id': self.node_id, 'run_id': self.run_id, 'model': self.model,
                'status': self.status, 'updated_at': time.time(), 'phase': self.phase,
                'task_seconds': round(elapsed, 3), 'requests': rows,
                'output_tokens': tokens if known else None,
                'average_tps': round(tokens / seconds, 2) if seconds > 0 else None,
                'effective_tps': round(tokens / elapsed, 2) if ended and len(known) == len(ended) and elapsed > 0 else None,
                'usage_complete': bool(ended) and len(known) == len(ended),
                'failed_requests': sum(r.get('finish_reason') not in ('stop', 'tool-calls', 'completed') for r in ended),
                'note': 'tok/s=接口返回的outputTokens÷请求耗时（含等待）；有效速度含预处理、工具、重试和复核。未返回用量时不估算；推理token不重复相加。'}

    def handle(self, data):
        if data.get('kind') == 'transport':
            error_log.update(self.run_id, {**data, 'phase': self.phase})
            return
        with self.lock:
            data = {**data, 'phase': self.phase}
            self.requests[data['request_id']] = data
            self.received_times[data['request_id']] = time.monotonic()
            if data['kind'] == 'end':
                rate = data.get('average_tps')
                print(f"[KKGO] Token速度：{rate:.2f} tok/s；输出{data['output_tokens']} token；请求{data['elapsed_seconds']:.3f}秒"
                      if rate is not None else '[KKGO] Token速度：接口未返回token用量，无法计算tok/s', flush=True)
            self.publish(save=data['kind'] in ('start', 'end'))

    def publish(self, save=False):
        payload = self.snapshot()
        if save:
            try:
                with _LOCK:
                    target = latest_file(self.node_id)
                    temp = target.with_suffix('.tmp')
                    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                    temp.replace(target)
                    history = speed_dir() / 'history.jsonl'
                    if history.exists() and history.stat().st_size > 5 * 1024 * 1024:
                        history.replace(history.with_suffix('.previous.jsonl'))
                    with history.open('a', encoding='utf-8') as handle:
                        handle.write(json.dumps(payload, ensure_ascii=False) + '\n')
            except OSError as exc:
                print(f'[KKGO] Token速度记录保存失败：{type(exc).__name__}', flush=True)

    def finish(self, status):
        with self.lock:
            # An externally killed stream may not emit its final side-channel line.
            for key, row in self.requests.items():
                if row['kind'] != 'end':
                    row['elapsed_seconds'] += time.monotonic() - self.received_times[key]
                    row.update(kind='end', finish_reason='interrupted', output_tokens=None, average_tps=None)
            self.status = status
            self.publish(save=True)
        with _LOCK:
            if _ACTIVE.get(str(self.node_id)) is self:
                _ACTIVE.pop(str(self.node_id), None)


class SpeedReader:
    """Tail the isolated worker's numeric-only side channel across SDK turns."""
    def __init__(self, file, callback=None):
        self.file = Path(file)
        self.callback = callback
        self.offset = self.file.stat().st_size if self.file.exists() else 0
        self.records = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._watch, name='kkgo-token-speed', daemon=True)

    def start(self):
        self.thread.start()

    def _drain(self):
        try:
            with self.file.open('rb') as handle:
                handle.seek(self.offset)
                while line := handle.readline():
                    if not line.endswith(b'\n'):
                        break
                    self.offset = handle.tell()
                    data = json.loads(line)
                    self.records.append(data)
                    if self.callback:
                        try:
                            self.callback(data)
                        except Exception:
                            pass
        except (OSError, ValueError):
            pass

    def _watch(self):
        while not self.stopped.wait(.2):
            self._drain()

    def close(self):
        self.stopped.set()
        self.thread.join()
        self._drain()
