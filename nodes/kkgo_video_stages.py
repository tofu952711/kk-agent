"""Expose real ComfyUI branches so SaveVideo and reverse can run together."""
from __future__ import annotations

import asyncio
import threading
from ..kkagent_bridge.cancellation import CancelScope, cancellable_to_thread

from comfy_api.latest import io
from ..kkagent_bridge.chain_b import iter_chain_b
from ..kkagent_bridge.duration_routing import route_duration
from ..kkagent_bridge.b_review import REVIEW_RULES
from ..kkagent_bridge.a_review import REVIEW_RULES as A_REVIEW_RULES
from .kkgo_video_agent import KKGO_FullVideoAgent, REVERSE_SETTINGS, fixed_review_config

ChainContext = io.Custom("KKGO_VIDEO_CONTEXT")


class VideoJob:
    """Opaque in-memory job; never serialize prompts, credentials or generator."""
    def __init__(self, stages=None, result=None, scope=None):
        self.scope = scope or CancelScope()
        self._stages = stages
        self._result = result
        self._error = None
        self._lock = threading.Lock()

    def __repr__(self):
        return "<KKGO video job>"

    def finish(self):
        with self._lock:
            self.scope.check()
            if self._error:
                raise self._error
            if self._result is not None:
                return self._result
            if self._stages is None:
                raise RuntimeError("反推任务已释放，请重新执行前面的入口节点")
            try:
                try:
                    next(self._stages)
                except StopIteration as finished:
                    self._result = finished.value
                    return self._result
                raise RuntimeError("反推阶段未正确结束")
            except BaseException as exc:
                self._error = exc
                raise
            finally:
                self._stages.close()
                self._stages = None
                self.scope.close()

    def close(self):
        self.scope.cancel()
        with self._lock:
            if self._stages is not None:
                self._stages.close()
                self._stages = None
            self.scope.close()

    def __del__(self):
        # Finalizers can run on Comfy's HTTP loop; cleanup must not block it.
        if getattr(self, "_stages", None) is not None:
            threading.Thread(target=self.close, name="video-job-cleanup", daemon=True).start()
        elif hasattr(self, "scope"):
            self.scope.close()


class KKGO_VideoSegments(KKGO_FullVideoAgent):
    # V3 caches schema metadata on classes. The original node can be inspected
    # first, so never inherit its eight-output cache for this nine-output node.
    _RETURN_TYPES = None
    _RETURN_NAMES = None
    _OUTPUT_IS_LIST = None
    _OUTPUT_TOOLTIPS = None
    _DESCRIPTION = None

    @classmethod
    def define_schema(cls):
        schema = super().define_schema()
        schema.node_id = "KKGO_VideoSegments"
        schema.display_name = "KK agent runninghub"
        schema.description = ("不足32秒走A：video1输出最长30秒参考视频，反推仍覆盖完整原片，独立反推与下游打码/保存并行；32至60秒执行B的AI工具分段。"
            "video1—video5连接SaveVideo，反推资料连接KK独立反推节点；两条分支可同时执行。"
            "本节点不自行保存视频，不指定保存目录。原有规则框和接口设置保持独立。")
        schema.outputs.append(ChainContext.Output(display_name="反推资料"))
        return schema

    @classmethod
    async def execute(cls, video, api_key, base_url=None, model=None, requirement="",
                      timeout_s=900, max_tokens=32768, asr_model_dir="", _observer=None,
                      api_protocol="deepseek-messages", rules_text="", keep_audio=True,
                      segment_rules_text="", reasoning_effort="high", stream_usage=True,
                      image1=None, image2=None, image3=None, image4=None, image5=None,
                      image6=None, image7=None, image8=None, image9=None, image10=None,
                      chain_b_reverse_rules_text="", chain_b_review_rules_text=REVIEW_RULES, review_ai=None, audio1=None, audio2=None, audio3=None,
                      chain_a_review_rules_text=A_REVIEW_RULES, review_api_key="", review_base_url=None):
        # Ignore provider overrides carried by older API-format workflows.
        base_url, model, timeout_s, max_tokens, asr_model_dir, api_protocol, reasoning_effort = (
            REVERSE_SETTINGS[name] for name in (
                "base_url", "model", "timeout_s", "max_tokens", "asr_model_dir", "api_protocol", "reasoning_effort"))
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("请在本节点的数值中填写密钥")
        review_ai = fixed_review_config(review_api_key, review_ai, review_base_url)
        routing = route_duration(video.get_duration())
        print(f"[KKGO] 并行入口：{routing['duration_seconds']}秒 → 链路{routing['chain']}", flush=True)
        images = (image1,image2,image3,image4,image5,image6,image7,image8,image9,image10)
        if routing["chain"] == "A":
            stages = cls.iter_chain_a(video, routing,
                api_key,base_url,model,requirement,rules_text,timeout_s,max_tokens,
                asr_model_dir,api_protocol,reasoning_effort,stream_usage,_observer,images,
                publish_video=True, audio_values=(audio1, audio2, audio3),
                chain_a_review_rules_text=chain_a_review_rules_text, review_ai=review_ai)
        else:
            stages = iter_chain_b(cls, video,routing,api_key,base_url,model,requirement,
                segment_rules_text,chain_b_reverse_rules_text,timeout_s,max_tokens,
                asr_model_dir,api_protocol,reasoning_effort,stream_usage,keep_audio,_observer,images,chain_b_review_rules_text,review_ai,(audio1,audio2,audio3))
        scope = CancelScope()
        def prepare():
            try:
                return next(stages)
            except BaseException:
                stages.close()
                scope.close()
                raise
        try:
            result = await cancellable_to_thread(prepare, scope=scope)
        except BaseException:
            scope.cancel()
            scope.close()
            raise
        return io.NodeOutput(*result, VideoJob(stages=stages, scope=scope))


class KKGO_VideoReverse(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="KKGO_VideoReverse", display_name="KK agent runninghub 独立反推",
            category="KK agent runninghub", is_output_node=True,
            description="接收入口的反推资料。A按A规则反推原片，B按B规则反推分段；两者均可与下游人脸处理及视频保存并行。保存路径由保存节点决定。",
            inputs=[ChainContext.Input("context", display_name="反推资料")],
            outputs=[io.String.Output(display_name="提示词"),io.String.Output(display_name="分析证据"),
                     io.String.Output(display_name="状态")])

    @classmethod
    async def execute(cls, context):
        if not isinstance(context, VideoJob):
            raise ValueError("请连接KKGO并行入口节点的「反推资料」输出")
        try:
            result = await cancellable_to_thread(context.finish, scope=context.scope)
        except BaseException:
            context.scope.cancel()
            threading.Thread(target=context.close, name="video-job-cleanup", daemon=True).start()
            raise
        return io.NodeOutput(*result[:3])
