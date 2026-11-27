"""Both node entry points honor editable URLs while retaining other fixed settings."""
import asyncio
import importlib
import os
from pathlib import Path
import sys
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("COMFYUI_PATH", str(ROOT.parents[1])))
sys.argv = [sys.argv[0]]
from comfy.cli_args import args
args.cpu = True
import comfy.model_management

package = types.ModuleType("fixed_reverse_check")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
agent = importlib.import_module(package.__name__ + ".nodes.kkgo_video_agent")
stages = importlib.import_module(package.__name__ + ".nodes.kkgo_video_stages")

expected = ("https://api.deepseek.com", "deepseek-flash", 1032, 65536, "", "openai-completions", "high")
fixed_names = ["base_url", "model", "timeout_s", "max_tokens", "asr_model_dir", "api_protocol", "reasoning_effort"]
reviewer = {"review_ai": "自定义", "api_key": "review-placeholder", "base_url": "https://review.invalid", "model": "review-model", "api_protocol": "codez"}

for cls in [agent.KKGO_FullVideoAgent, stages.KKGO_VideoSegments]:
    inputs = cls.define_schema().inputs
    names = [item.id for item in inputs]
    assert set(names).intersection(fixed_names) == {"base_url", "model"}, names
    reverse_url = next(item for item in inputs if item.id == "base_url")
    review_url = next(item for item in inputs if item.id == "review_base_url")
    assert reverse_url.display_name == "反推 AI Base URL" and reverse_url.optional
    assert reverse_url.default == agent.REVERSE_SETTINGS["base_url"]
    assert review_url.display_name == "审核 AI Base URL"
    key = next(item for item in inputs if item.id == "api_key")
    assert key.display_name == "数值" and key.default == ""
    assert "review_ai" not in names and "review_api_key" in names and "requirement" in names
    review_key = next(item for item in inputs if item.id == "review_api_key")
    assert review_key.display_name == "数值" and review_key.default == ""

class Video:
    def __init__(self, duration): self.duration = duration
    def get_duration(self): return self.duration

async def main():
    observed = []
    def capture(*args, **kwargs):
        observed.append((args, kwargs))
        return ("text", "evidence", "status", None, None, None, None, None, [], '{"frames":[]}')
    def generate(*args, **kwargs):
        capture(*args, **kwargs)
        yield ("text", "evidence", "status", None, None, None, None, None)
        return ("text", "evidence", "status")
    override = dict(api_key="local-placeholder", base_url="https://override.invalid", model="override-model", timeout_s=30,
                    max_tokens=256, asr_model_dir="/override", api_protocol="deepseek-messages", reasoning_effort="low",
                    requirement="保留要求", rules_text="A规则", segment_rules_text="分段规则", chain_b_reverse_rules_text="B规则",
                    review_ai=reviewer, review_api_key="new-placeholder", review_base_url="https://chosen.invalid/v1", review_model="chosen-review-model")
    for duration in [3, 33]:
        for cls in [agent.KKGO_FullVideoAgent, stages.KKGO_VideoSegments]:
            with patch.object(cls, "run_chain_a", side_effect=capture), patch.object(cls, "run_chain_b", side_effect=capture), \
                 patch.object(cls, "iter_chain_a", side_effect=generate), patch.object(stages, "iter_chain_b", side_effect=generate):
                result = await cls.execute(Video(duration), **override)
                if cls is stages.KKGO_VideoSegments:
                    result[8].close()
            positional, keywords = observed[-1]
            start = 1 if duration == 33 and cls is stages.KKGO_VideoSegments else 0
            positional = positional[start:]
            if duration == 3:
                actual = (positional[3], positional[4], positional[7], positional[8], positional[9], positional[10], positional[11])
            else:
                actual = (positional[3], positional[4], positional[8], positional[9], positional[10], positional[11], positional[12])
            assert actual == (override["base_url"], override["model"], *expected[2:]), (cls.__name__, duration, actual)
            assert positional[2] == "local-placeholder"
            assert positional[5] == "保留要求"
            actual_review = next((item for item in positional if isinstance(item, dict) and item.get("review_ai")), keywords.get("review_ai"))
            assert actual_review == {"review_ai": "自定义", "api_key": "new-placeholder", **agent.REVIEW_SETTINGS, "base_url": "https://chosen.invalid/v1", "model": "chosen-review-model"}
    assert agent.resolve_reverse_base_url(None) == "https://api.deepseek.com"
    assert agent.resolve_reverse_base_url("  https://chosen.invalid/v1  ") == "https://chosen.invalid/v1"
    for invalid in ("", "   ", 123):
        try:
            agent.resolve_reverse_base_url(invalid)
            raise AssertionError("invalid Base URL accepted")
        except ValueError as exc:
            assert "反推 AI Base URL" in str(exc)
    assert agent.fixed_review_config("new-placeholder", reviewer)["api_key"] == "new-placeholder"
    assert agent.fixed_review_config("", reviewer)["model"] == "gpt-6.1-sol"
    assert agent.fixed_review_config("", reviewer)["base_url"] == reviewer["base_url"]
    assert agent.fixed_review_config("key", None, "https://chosen.invalid")["base_url"] == "https://chosen.invalid"
    try:
        agent.fixed_review_config("", {"review_ai": "沿用当前AI"})
        raise AssertionError("missing review key accepted")
    except ValueError as exc:
        assert "审核区域的数值" in str(exc)
    for cls in [agent.KKGO_FullVideoAgent, stages.KKGO_VideoSegments]:
        try:
            await cls.execute(Video(3), api_key="")
            raise AssertionError("empty credential accepted")
        except ValueError as exc:
            assert "数值" in str(exc)
    for invalid in ("", "   ", 123):
        for label in ("反推 AI 模型名称", "审核 AI 模型名称"):
            try:
                agent.resolve_model_name(invalid, "default-model", label)
                raise AssertionError("invalid model accepted")
            except ValueError as exc:
                assert label in str(exc)
    print("Passed editable models and URLs in both schemas and A/B execution, fixed non-URL settings, reviewer routing and empty-value checks.")

if __name__ == "__main__":
    asyncio.run(main())
