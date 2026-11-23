# Third-party notices

Python business/media/node UI code derives from the local ComfyUI-KKAgent-Go / ComfyUI-DSH-Bridge source. The original MIT LICENSE is retained.

The new task-local Python model/provider/MCP modules do not distribute or launch Go, DeepSeek Harness, Node.js, or a private Python runtime.

ONNX assets and original source/license notices are included in `models/sensevoice/` and `models/speaker-diarization/`. Their licenses apply separately from the Python code. Some upstream notices retain references to the original SDK implementation; those notices do not describe a runtime dependency of this plugin.

The complete offline release includes Linux FFmpeg and FFprobe from BtbN/FFmpeg-Builds, with their original license and build/source metadata in vendor/ffmpeg/linux-x86_64/. They are separate executables and retain their own licensing.

Python libraries are installed through `requirements.txt` into the ComfyUI environment and retain their own licenses.

The optional frontend RH declaration follows the public example at https://github.com/HM-RunningHub/ComfyUI_RH_OpenAPI/blob/main/web/js/rh_panel.js . This is a compatibility reference, not RunningHub platform certification.

Full offline bundle: Linux FFmpeg/FFprobe are supplied from BtbN/FFmpeg-Builds. The original LICENSE.txt, build metadata, upstream checksums and source/build links are in vendor/ffmpeg/linux-x86_64/. Python wheel archives include their upstream .dist-info license records. ONNX licenses remain separate.
