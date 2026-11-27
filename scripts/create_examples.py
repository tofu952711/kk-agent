"""Create a minimal template from current schemas; never copy personal workflows or keys."""
import importlib
import json
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]


def main():
    comfy_root = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent.parent
    sys.path.insert(0, str(comfy_root))
    sys.argv = [sys.argv[0]]
    from comfy.cli_args import args
    args.cpu = True
    package = types.ModuleType("kk_example_generator")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    stages = importlib.import_module(package.__name__ + ".nodes.kkgo_video_stages")
    schema = stages.KKGO_VideoSegments.define_schema()
    defaults = {item.id: item.default for item in schema.inputs if hasattr(item, "default")}
    defaults.update(requirement="按原片顺序分析画面、动作、镜头和对白；不虚构无法核实的内容。",
        rules_text="你是视频反推助手。依据原视频与证据，用简体中文描述人物、场景、动作时间线、运镜和对白。区分观察事实与推测。",
        segment_rules_text="按所选时长模式分段：32秒模式每段5至32秒，17秒模式每段5至17秒；分为2—5段，连续覆盖原片。按内容边界选择切点，避免截断关键动作和词语；必须调用set_video_segments工具实际裁切。",
        chain_b_reverse_rules_text="只描述当前分段，用简体中文描述人物、场景、动作时间线、运镜和对白，沿用共用设定，按要求引用提供的图片编号。")
    widget_values = list(defaults.values())
    input_names = [item.id for item in schema.inputs if not hasattr(item, "default")]
    inputs = [{"name": name, "type": "VIDEO" if name == "video" else "AUDIO" if name.startswith("audio") else "IMAGE",
               "link": 1 if name == "video" else None} for name in input_names]
    output_types = ["STRING"] * 3 + ["VIDEO"] * 5 + ["KKGO_VIDEO_CONTEXT", "IMAGE", "STRING"] + ["IMAGE"] * 5
    outputs = [{"name": output.display_name, "type": kind, "slot_index": i,
                "links": [2, 4] if i == 8 else [6] if i == 3 else []} for i, (output, kind) in enumerate(zip(schema.outputs, output_types))]

    def node(identifier, kind, position, title, inputs, outputs, widgets):
        return {"id": identifier, "type": kind, "pos": position, "size": [380, 240], "flags": {},
                "order": identifier - 1, "mode": 0, "title": title, "inputs": inputs, "outputs": outputs,
                "properties": {"Node name for S&R": kind}, "widgets_values": widgets}

    graph = {"version": 0.4, "last_node_id": 7, "last_link_id": 6, "nodes": [
        node(1, "LoadVideo", [20, 100], "上传原视频（最长60秒）", [],
             [{"name": "VIDEO", "type": "VIDEO", "links": [1], "slot_index": 0}], ["your_video.mp4"]),
        node(2, "KKGO_VideoSegments", [450, 100], "kk agent 2：填写反推与审核密钥", inputs, outputs, widget_values),
        node(3, "KKGO_VideoReverse", [1000, 100], "独立反推与审核",
             [{"name": "context", "type": "KKGO_VIDEO_CONTEXT", "link": 2}],
             [{"name": "提示词", "type": "STRING", "links": [3], "slot_index": 0}], []),
        node(4, "PreviewAny", [1450, 100], "查看提示词",
             [{"name": "source", "type": "*", "link": 3}],
             [{"name": "STRING", "type": "STRING", "links": [], "slot_index": 0}], []),
        node(5, "KKGO_VideoFrames", [1000, 500], "kk agent 2 独立抽帧",
             [{"name":"context", "type":"KKGO_VIDEO_CONTEXT", "link":4}],
             [{"name":output.display_name, "type":"STRING" if i==1 else "IMAGE", "slot_index":i,
               "links":[5] if i==0 else []} for i,output in enumerate(stages.KKGO_VideoFrames.define_schema().outputs)], []),
        node(6, "SaveImage", [1450, 500], "保存各段参考图",
             [{"name":"images", "type":"IMAGE", "link":5}], [], ["kk-agent-2/frames"]),
        node(7, "SaveVideo", [1000, 850], "保存video1（与选帧、反推异步）",
             [{"name":"video", "type":"VIDEO", "link":6}],
             [{"name":"video", "type":"VIDEO", "links":[], "slot_index":0}],
             ["kk-agent-2/video1", "auto", "auto"]),
    ], "links": [[1,1,0,2,0,"VIDEO"],[2,2,8,3,0,"KKGO_VIDEO_CONTEXT"],[3,3,0,4,0,"STRING"],
                  [4,2,8,5,0,"KKGO_VIDEO_CONTEXT"],[5,5,0,6,0,"IMAGE"],[6,2,3,7,0,"VIDEO"]],
       "groups": [], "config": {}, "extra": {"ds": {"scale": 0.7, "offset": [20, 20]}}}
    target = ROOT / "example_workflows/KKagent_basic.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n")
    print(target)


if __name__ == "__main__":
    main()
