"""MCP stdio server launched by Harness; only the staged video is accessible."""
from __future__ import annotations
import argparse
import base64
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from media_tools import MediaTools, definitions, text_result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path)
    args = parser.parse_args()
    # Only protocol JSON goes to stdout, including during native ASR imports.
    protocol = sys.stdout
    sys.stdout = sys.stderr
    tools = MediaTools(args.video, args.output, args.model_dir)
    def send(message):
        protocol.write(json.dumps(message, ensure_ascii=False) + "\n")
        protocol.flush()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            method = request.get("method")
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "kkgo-video-analysis", "version": "0.8.3"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                policy = tools._read_json("duration_policy.json")
                result = {"tools": definitions(allow_segments=not policy or policy.get("phase") == "segment")}
            elif method == "tools/call":
                name = request["params"]["name"]
                arguments = request["params"].get("arguments") or {}
                with (args.output / "tool_activity.jsonl").open("a", encoding="utf-8") as activity:
                    activity.write(json.dumps({"tool": name, "phase": "start", "time": time.strftime("%Y-%m-%d %H:%M:%S")}) + "\n")
                if name == "inspect_video":
                    result = text_result(tools.info())
                elif name == "detect_shots":
                    result = text_result(tools.shots(**arguments))
                elif name == "get_frames":
                    meta, image = tools.frames(**arguments)
                    result = text_result(meta)
                    result["content"].append({"type": "image", "data": base64.b64encode(image).decode(), "mimeType": "image/jpeg"})
                elif name == "transcribe_audio":
                    result = text_result(tools.transcribe())
                elif name == "analyze_voices":
                    result = text_result(tools.analyze_voices(**arguments))
                elif name == "diarize_speakers":
                    result = text_result(tools.diarize_speakers(**arguments))
                elif name == "set_video_segments":
                    result = text_result(tools.set_video_segments(**arguments))
                else:
                    raise ValueError("Unknown tool")
                with (args.output / "tool_activity.jsonl").open("a", encoding="utf-8") as activity:
                    activity.write(json.dumps({"tool": name, "phase": "done", "time": time.strftime("%Y-%m-%d %H:%M:%S")}) + "\n")
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
