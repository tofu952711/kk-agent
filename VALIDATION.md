# KK agent runninghub 验证记录

日期：2026-11-23。开发机为 Windows，目标为 Linux ComfyUI。以下离线结果不能替代 RunningHub 容器内的实机验收。

## 已通过

- `tests/test_runtime.py`：16 项通过。覆盖三种协议的实际本地 HTTP 请求、文字框原文保留、空用户框、并行会话隔离、工具续轮、重复调用去重、JSON Schema 参数校验、未开放工具拒绝、空/截断结果拒绝、取消与超时清理、流式工具参数、Messages thinking signature、Responses encrypted reasoning。
- `tests/test_process_cleanup.py`：本机真实父子进程清理通过；Linux 进程组 leader 已退出后的清理测试因缺少 Linux 内核而跳过。
- `tests/verify_workflows.py`：13 项检查通过。使用真实 3 秒 A 视频、真实 33 秒 B 视频、本地模拟模型和真实 FFmpeg。覆盖时长边界、规则按链路读取、A/B 并行重叠、用户/工具上下文隔离、视频输出、两段真实裁切、并行子任务、两套审核、重复读取 VideoJob 不重复执行、一个任务清理不影响另一个任务、用量记录分离、最终连接/工具释放和无剩余所属子进程。
- 任务结束后 `active_turns=0`、`media_workers=0`；进程树复查没有本测试创建的残留子进程。
- 0.2.0 曾与 Windows GO 版完成逐字段对照；0.2.1 按用户要求移除七个主连接/运行设置输入，主密钥显示名改为“数值”。节点 ID、输出及审核字段保留，旧图通过前端迁移。
- 所有 Python 文件语法检查通过；4 个 JavaScript 文件通过 Node 语法检查。
- Linux x86_64 / CPython 3.12 的 `sherpa-onnx==2.0.1`、`sherpa-onnx-core==2.0.1`、`jsonschema==4.26.0` 及其依赖通过 pip 二进制 wheel 解析。未修改原 ComfyUI Python 环境；新增本机测试依赖装在交付目录外的隔离文件夹。
- 源 Linux 插件的已读取文件哈希复查无变化。原已安装插件未修改。
- 所有模型测试均使用本地占位 Key，付费模型调用为 0。

机器可读摘要见 `validation-summary.json`。

## 待在 Linux / RunningHub 验证

- Linux 下原生库实际导入、FFmpeg 编码、SenseVoice/说话人模型推理与整个工作流。
- Linux 专有进程组清理测试。
- 平台 ComfyUI 版本、任务取消行为、前端 `rh` 声明、自定义布局及 WebSocket 用量显示。
- 平台网络出站策略、请求时限和公开工作流审核。
- 如果平台强制统一模型通道，需要按其规范单独接入。目前按用户要求保留原 Key/Base URL/模型输入方式。

## 0.2.1 固定反推配置验证（2026-11-24）

- `tests/test_fixed_reverse.py` 在 Linux 通过：两个入口的 A/B 分支均强制使用固定设置，旧 API 参数无法覆盖；自定义审核配置保留；主密钥不内置，空值提示“数值”。
- `tests/check_fixed_widgets.mjs` 通过：旧图的规则、密钥、动态审核子字段按原值迁移，新图再次加载不重复迁移；删除固定字段旧连线，同时保留规则连线及其正确插口。
- `tests/verify_workflows.py` 在 Linux 使用本地模拟模型、真实 FFmpeg 完成13项 A/B 流程检查，未调用付费模型。

## 0.2.2 固定审核配置验证（2026-11-24）

- 两个节点入口的A/B分支都使用固定审核地址、模型及协议，独立审核数值优先于旧图密钥；旧图不能通过自定义配置覆盖固定模型。
- 0.2.0与0.2.1的动态审核字段迁移为单个数值，保留A审核规则和原独立审核密钥，旧“沿用当前AI”模式迁移为空白审核数值。
- 审核密钥连线迁移，移除审核模型等固定字段的旧连线；上方主数值与下方审核数值独立。

## 建议平台验收命令

```bash
python scripts/check_linux.py
python -B tests/test_runtime.py
python -B tests/test_process_cleanup.py
COMFYUI_PATH=/path/to/ComfyUI python -B tests/verify_workflows.py
```

通过后再用有音轨测试素材验收 A/B、语音识别、说话人分析和取消操作。测试不会自动使用节点内保存的供应商密钥。

## 后续 Linux 实机验收（2026-11-23）

已在本机 WSL Ubuntu 24.04.5、Python 3.12.3、Linux ComfyUI 0.45.0 的 CPU 模式执行：16 项运行时、2 项 Linux 真实进程清理、13 项 A/B 并行流程检查全部通过。真实 ComfyUI HTTP 队列执行一次 A 链路及审核成功，结束后活动请求和媒体工具进程为 0。SenseVoice 静音解码及说话人 ONNX 模型加载通过（不代表真实语音准确率验收）。没有下载生图模型或调用付费接口。

Linux 测试脚本已预先导入 comfy.model_management，避免两个离线测试线程同时初始化 ComfyUI 核心。插件生产代码未因此修改。

当前 WSL GPU CUDA 初始化失败，专用测试环境采用 CPU；这不影响本插件的 CPU 视频/语音和外部模型接口流程。RunningHub 容器与真实供应商网关联调仍未进行。

## 0.2.3 移除用量显示（2026-11-24）

- 删除节点Token显示及专用前端依赖，不注册用量/结果补读接口，不再推送显示事件。
- API调用、返回用量的后台记录、规则和视频预览不改动。

## 0.2.4 审核地址输入

“数值2”恢复审核地址编辑；验证两个入口A/B实际传入所选审核地址，模型及协议仍固定。旧工作流迁移同时保留审核Key、地址和A审核规则。生命周期测试中此前已知的不匹配不在本次修改范围。

## GitHub deployment preparation (2026-11-24)

- Re-ran `tests/test_node_lifecycle.py`: 8 tests, 3 passed, 2 failures, 3 errors. The fixture omits `review_api_key`; another case calls the missing `VideoJob.prepare()` and expects `JOB_CLEANUP`. These special lifecycle paths have not passed acceptance.
- Added installation-time download of the fixed SenseVoice asset from this repository release, with SHA-256 validation. Existing local models are kept; node runtime does not download models.
- Installer checks covered an existing valid model, a successful download, skipping a repeated download, rejecting a wrong hash, preserving the previous file on failure, and removing temporary downloads.
