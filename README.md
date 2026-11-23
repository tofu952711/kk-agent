# KK agent runninghub

面向 **Linux 版 ComfyUI / RunningHub** 的 Python 视频反推插件。

保留 A/B 分段、素材准备、并行反推、最终审核及视频输出流程。模型调用及工具循环使用 Python。

## 安装到 Linux ComfyUI

1. 从 GitHub 克隆到 Linux ComfyUI 的 `custom_nodes/`。同一台 ComfyUI 只保留一个注册这些节点 ID 的插件目录：

```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/tofu952711/kk-agent-runninghub.git
```
2. 由平台在系统环境安装 `ffmpeg` 和 `ffprobe`，FFmpeg 需要包含 `libx264` 编码器。
3. 用**启动该 ComfyUI 的同一个 Python 环境**安装依赖并检查：

```bash
cd /path/to/ComfyUI/custom_nodes/kk-agent-runninghub
PYTHON=/path/to/comfy-venv/bin/python bash install-linux.sh
```

ComfyUI-Manager 安装时可执行根目录 `install.py` 获取语音模型。平台若只安装 `requirements.txt`，还必须在部署阶段额外执行 `python install.py`；仅克隆代码不足以备齐模型。下载需要访问本仓库的 GitHub Release，按固定 SHA-256 校验；已有正确模型时跳过下载。`install-linux.sh` 会依次安装依赖、获取模型、检查环境。`bash install-linux.sh --check` 只检查，不安装或下载。

依赖与模型已准备好时只做检查：

```bash
/path/to/comfy-venv/bin/python scripts/check_linux.py
```

4. 重启 ComfyUI 并强制刷新网页，在 `KK agent runninghub` 分类下添加节点。运行前填写上下两个“数值”及各阶段规则。

需要 Python 3.10+ 和支持 `comfy_api.latest` 的 ComfyUI。CPython 3.12 Linux x86_64 的新增依赖已完成 wheel 解析检查；其他 Python/CPU 组合以平台安装检查为准。

## 节点与旧工作流

| 显示名称 | 保留的节点 ID |
|---|---|
| KK agent runninghub | `KKGO_VideoSegments` |
| KK agent runninghub 完整视频反推 | `KKGO_FullVideoAgent` |
| KK agent runninghub 独立反推 | `KKGO_VideoReverse` |

保留 `KKGO_*` ID、输出与 `KKGO_VIDEO_CONTEXT` 类型，和使用 Go 无关。0.2.1 移除七个主连接与运行设置输入，0.2.2 将动态审核配置替换为单个审核数值。加载旧图时保留密钥、要求、规则和参考素材，移除已固定设置的旧连线；原审核密钥的连线迁移到审核数值。**同一台 ComfyUI 仅启用一个使用这些 ID 的插件版本**，避免重复注册。

入口的 `video1`—`video5` 连接原有保存/视频处理分支；`反推资料` 继续连接独立反推节点。需要获得提示词时必须执行独立反推节点，保持原有接线方式。

不足 32 秒走 A，32—60 秒走 B。A 保留完整原片分析和最长 30 秒参考输出；B 由 AI 调用工具实际裁出 2—5 段，然后完成共用设定、并行反推及审核。规则正文与用户要求仍分别传入；审核模型配置方式保持原样。

## 按任务启动和清理

- 导入插件不会启动后端服务，也不监听 8191、8192 或其他插件服务端口。
- 模型请求直接由任务内的 Python 执行，媒体工具通过私有 stdio 管道运行。
- 一轮智能体调用包含它需要的多轮模型请求与工具结果续传；工具进程在这轮调用结束后关闭。后续审核和独立子任务使用自己的上下文。
- 已执行的模型/工具调用在正常结束、异常、超时或主动取消时关闭请求连接并清理所属媒体工具进程；Linux 工具内的编码器继承工具的专有进程组，方便整体清理。
- 并行任务各自持有历史、规则和工具目录。一个任务结束不会关闭其他任务或 ComfyUI。
- 最多同时保留两个媒体工具进程；语音模型继续按需在工具/预处理进程内加载，进程退出后释放。
- 中断的模型流不会自动重放；仅尚未成功返回的 HTTP 408、429、5xx 状态在任务截止时间内退避重试。

任务临时素材随对应上下文退出删除。入口已执行但反推资料未被消费的分支，任务对象可能被ComfyUI缓存持有，及时清理仍需完成专项验收。保留现有分析结果、审核证据及数值用量记录，不自动删除输出视频或平台任务历史。平台直接强制终止容器时，进程清理由平台容器生命周期负责。

## 模型与依赖

仓库直接包含分词表、说话人模型及来源/许可证文件；约239 MB的SenseVoice权重超过GitHub普通文件100 MB限制，存放于本仓库固定的 `models-v1` Release，安装时获取：

- `models/sensevoice/model.int8.onnx` 和 `tokens.txt`；
- `models/speaker-diarization/segmentation.onnx` 和 `speaker_embedding.onnx`；
- 原模型来源和许可证文件。

运行节点时不下载语音模型；安装阶段须先运行 `install.py`，或将同一校验值的模型手动放入指定位置。语音计算使用 CPU。新增依赖固定为 `sherpa-onnx==2.0.1`、`jsonschema==4.26.0`；torch、numpy、Pillow、av、aiohttp、psutil 沿用 ComfyUI 环境。

## RunningHub 前端与模型通道

保留节点前端布局脚本并添加公开 RH 插件所使用的 `rh: {type: "nodes", nodes: "all"}` 声明。已移除全局浮动面板和节点底部的Token速度、用量、耗时显示；保留节点布局和视频预览。平台可设置 `KKGO_ENABLE_FRONTEND=0` 禁止注册自定义前端；API 格式工作流的后端执行不依赖这些脚本，前端布局与旧图迁移辅助将不加载。

0.2.3 删除 `/kkagent_runninghub/token_speed`、`/kkagent_runninghub/last` 补读接口及 `kkgo_token_speed` 实时事件推送。后台仍保留API返回用量和分析记录，正常反推、审核、取消清理和结果输出不依赖显示组件。`/kkagent_runninghub/status` 仅供运行状态诊断。

**主反推地址、两侧模型名称及协议固定；审核地址通过“数值2”填写，两个密钥分别在“数值”中填写。本插件没有接入 RunningHub 专有模型计费/鉴权通道。** 如果平台审核要求统一模型通道，仍需其提供协议/鉴权规范才能完成该部分接入；固定地址不等于接入平台模型通道。

## 验证

本机离线验证记录见 `VALIDATION.md`。新增生命周期测试此前8项中3项通过、5项未通过，包含测试缺少审核占位密钥、调用当前不存在的接口等问题；仅保存分支、下游失败等特殊清理场景尚未验收，不能宣称整组通过。Linux 部署后运行：

```bash
python scripts/check_linux.py
python -B tests/test_runtime.py
python -B tests/test_process_cleanup.py
COMFYUI_PATH=/path/to/ComfyUI python -B tests/verify_workflows.py
```

测试使用占位 Key、本地模拟模型和真实 FFmpeg，**不会调用付费模型**。此版本尚未在 RunningHub 容器中完成实机联调或审核，不能将本机验证等同于平台上线成功。

## 0.2.4 审核地址

审核区域恢复可填写的地址输入，显示为“数值2”，默认 https://api.zdjapi.com。审核模型 gpt-6.1-sol、协议 openai-completions 和高思考等级保持固定。旧独立审核配置保留原地址，0.2.2/0.2.3旧图自动补入默认地址；主反推配置不变。
