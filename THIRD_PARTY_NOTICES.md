# Third-party notices

Python business/media/node UI code derives from the local ComfyUI-KKAgent-Go / ComfyUI-DSH-Bridge source. The original MIT LICENSE is retained.

The new task-local Python model/provider/MCP modules do not distribute or launch Go, DeepSeek Harness, Node.js, or a private Python runtime.

ONNX assets and original source/license notices are included in `models/sensevoice/` and `models/speaker-diarization/`. Their licenses apply separately from the Python code. Some upstream notices retain references to the original SDK implementation; those notices do not describe a runtime dependency of this plugin.

FFmpeg is supplied by the Linux platform and is not bundled. Its build and license obligations are managed by the platform distribution.

Python libraries are installed through `requirements.txt` into the ComfyUI environment and retain their own licenses.

The optional frontend RH declaration follows the public example at https://github.com/HM-RunningHub/ComfyUI_RH_OpenAPI/blob/main/web/js/rh_panel.js . This is a compatibility reference, not RunningHub platform certification.
