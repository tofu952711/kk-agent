"""B-only MCP worker; each child has a scoped, private media directory."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys
import time

# Run as a script through the same ComfyUI Python used by the existing tools.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kkagent_bridge.b_segment_media import SegmentMediaTools
from kkagent_bridge.media_tools import definitions, text_result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path)
    args = parser.parse_args()
    protocol = sys.stdout
    sys.stdout = sys.stderr
    tools = SegmentMediaTools(args.video, args.output, args.model_dir)

    def send(value):
        protocol.write(json.dumps(value, ensure_ascii=False) + "\n")
        protocol.flush()

    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            method = request.get("method")
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "kkgo-b-segment-analysis", "version": "1"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": definitions(allow_segments=False)}
            elif method == "tools/call":
                name = request["params"]["name"]
                arguments = request["params"].get("arguments") or {}
                methods = {"inspect_video": tools.info, "detect_shots": tools.shots,
                           "transcribe_audio": tools.transcribe, "analyze_voices": tools.analyze_voices,
                           "diarize_speakers": tools.diarize_speakers}
                began = time.monotonic()
                if name == "get_frames":
                    meta, raw = tools.frames(**arguments)
                    result = text_result(meta)
                    result["content"].append({"type": "image", "mimeType": "image/jpeg",
                                               "data": base64.b64encode(raw).decode()})
                elif name in methods:
                    result = text_result(methods[name](**arguments))
                else:
                    raise ValueError("本B子任务未提供此工具")
                with (args.output / "tool_activity.jsonl").open("a", encoding="utf-8") as log:
                    log.write(json.dumps({"tool": name, "phase": "done", "segment_index": tools.scope["index"],
                                          "seconds": round(time.monotonic() - began, 3)}, ensure_ascii=False) + "\n")
            else:
                send({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method not found"}})
                continue
            send({"jsonrpc": "2.0", "id": request["id"], "result": result})
        except Exception as exc:
            if "id" in request:
                send({"jsonrpc": "2.0", "id": request["id"], "result": {
                    "isError": True, "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}]}})


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    main()
