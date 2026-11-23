# 完整交付包验证

版本0.2.5，2026-11-24。目标为 Linux x86_64、glibc >=2.28、CPython 3.10—3.13 与现有 ComfyUI。

## 本次已验证

- 原有三个 ONNX 权重和分词表均包含在包内；完整文件 SHA-256 见 bundle-manifest.json。
- FFmpeg/FFprobe 上游压缩包校验通过；自带二进制可执行、libx264实际编码通过；动态依赖仅为glibc运行库及libgcc_s，最大glibc符号版本2.28。
- 新建Python 3.12虚拟环境，使用 --no-index、包内wheel与固定依赖安装成功；没有从现有环境复用插件新增依赖。安装后仅接入既有ComfyUI基础依赖供集成检查，不另行打包或替换PyTorch/CUDA。
- CPython 3.10、3.11、3.12、3.13的wheel依赖均在本地无索引解析成功。实际导入与推理运行验证在Python 3.12完成，其他三种Python版本尚未实机运行。
- 16项模型运行时测试、2项Linux进程清理测试通过。
- 使用本地模拟HTTP接口、真实FFmpeg的13项A/B分段、并行反推、审核与正常路径清理检查通过。未调用付费模型。
- 实际SenseVoice静音解码、说话人分段/嵌入模型加载成功。静音ASR产生了“没。”，这仅验证可运行，不代表静音识别或真实语音准确率合格。
- 交付版不包含本地debug_export、任务数据、个人密钥或桌面目录。

## 已知限制

- 新增test_node_lifecycle.py此前为8项中3项通过、2项失败、3项错误：fixture缺少审核占位密钥，另有测试引用当前不存在的VideoJob.prepare和JOB_CLEANUP；本次依赖打包不修复这些问题。
- Chat工具补帧未作为实际图片续传、共用场景与原片动作偏离等提示词质量问题仍待处理，本次不修改反推行为。
- RunningHub容器尚未完成实机安装、网络与任务取消联调；平台需具备所声明的Linux、glibc、Python和ComfyUI基础环境。
- 安装本身不联网；实际反推和审核接口仍需可用网络与用户凭据。
