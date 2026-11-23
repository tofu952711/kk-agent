# KK agent runninghub

面向 Linux ComfyUI / RunningHub 的视频反推插件，包含 A/B 分段、并行反推、审核及视频输出。

## 完整包安装

将交付的 `ComfyUI-KKAgent-RunningHub` 目录放入 ComfyUI 的 `custom_nodes/`，使用启动 ComfyUI 的 Python 执行：

```bash
cd /path/to/ComfyUI/custom_nodes/ComfyUI-KKAgent-RunningHub
/path/to/comfy-venv/bin/python install.py
```

安装只使用包内文件，不下载模型、媒体工具或 Python 依赖，无需 apt、sudo 或编译。安装完成后重启 ComfyUI，并刷新网页。

## 已包含

- SenseVoice 语音模型、分词表，以及两个说话人分析模型。
- Linux x86_64 FFmpeg / FFprobe，含 libx264 编码器；插件优先使用自带工具。
- CPython 3.10、3.11、3.12、3.13 对应的新增依赖 wheel 与全部传递依赖。
- 插件代码、前端、第三方来源和许可证、文件校验清单及验证记录。

完整包见本仓库 Releases。GitHub 的自动 Source code ZIP / 单独 git clone 仅包含源码，不能代替完整交付包。完整包获取后，安装过程无需联网。实际模型 API 调用仍需网络及用户填写的凭据。

## 环境与检查

目标环境：Linux x86_64、glibc 2.28 或更高、CPython 3.10—3.13，以及支持 `comfy_api.latest` 的现有 ComfyUI。ComfyUI 自带的 torch、numpy、Pillow、av、aiohttp、psutil 沿用平台环境；本插件不会替换平台的 CUDA / PyTorch。

```bash
/path/to/comfy-venv/bin/python scripts/check_linux.py
```

同一台 ComfyUI 只启用一个注册 `KKGO_*` 节点的版本。完整包不包含个人密钥、任务素材、运行历史或临时桌面调试导出功能。

验证范围和已知未通过项目见 `VALIDATION.md`。离线安装成功不代表 RunningHub 容器已完成实机验收，也不代表尚待处理的提示词准确性及特殊生命周期问题已经修复。
