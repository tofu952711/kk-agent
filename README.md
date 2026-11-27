# kk agent 2


## 0.2.20：kk agent 2

- 插件、节点及分类统一显示为 **kk agent 2**。继续使用原仓库、安装目录和 `KKGO_*` 节点 ID，旧工作流可升级。
- 32秒模式：不足32秒走A，32—60秒走B，参考视频最长30秒。17秒模式：不足17秒走A，17—60秒走B，参考视频最长15秒。反推仍分析完整原片/完整分段。
- 反推和审核区域均可编辑 Base URL、模型名称和密钥，填写值用于实际模型调用。
- B分段视频准备完成立即输出到原生 SaveVideo；后台继续统筹、并行选帧、并行反推及最终审核，反推不读取保存节点生成的文件。
- 新增 **kk agent 2 独立抽帧**（`KKGO_VideoFrames`）：图片选好即可交给保存/编辑分支，不等待最终审核。入口只保留原有文本、VIDEO和反推资料输出；旧工作流的图片/抽帧信息连线在加载时自动迁移，旧API工作流也兼容。
- 多个原生 SaveVideo/SaveImage 之间仍由 ComfyUI 依次调度；异步发生在保存分支与后台AI任务之间。
- 任务失败日志记录接口响应状态、有限内容片段和异常，密钥隐藏；新任务清除旧记录，成功任务不保留报错日志。

更新后重启 ComfyUI、强制刷新网页，再重新打开工作流。已有保存目录、格式和编码设置保持原值。

Linux ComfyUI / RunningHub video analysis, segmentation, parallel reverse prompting, review and reference-frame outputs. Models and media are processed locally; configured remote AI requests require your own API credentials. RunningHub certification is not implied.

## Source / ComfyUI Manager installation (0.2.18+)

Use the Python environment that runs ComfyUI. Linux x86_64, Python 3.10+, a ComfyUI version supporting `comfy_api.latest` (tested: 0.45.0), and FFmpeg with FFprobe are required. Keep the existing ComfyUI PyTorch/CUDA environment.

```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/tofu952711/kk-agent.git ComfyUI-KKAgent-RunningHub
/path/to/comfy-venv/bin/python -m pip install -r ComfyUI-KKAgent-RunningHub/requirements.txt
/path/to/comfy-venv/bin/python ComfyUI-KKAgent-RunningHub/install.py --comfy-root /path/to/ComfyUI
```

Alternatively unzip the `kk-agent-2-0.2.20-source-linux.zip` release into `custom_nodes/`. In the Manager git-URL installation flow, Manager installs `requirements.txt` and runs the `install.py` hook. The source hook does not run pip, does not require a bundle manifest or local wheels, and does not replace PyTorch/CUDA. It checks prerequisites, verifies existing model hashes and downloads only missing model assets from their original sources or SHA256-locked existing GitHub mirrors.

Install system FFmpeg/FFprobe before installing models. Existing bundled tools remain supported; explicit `KKGO_FFMPEG` and `KKGO_FFPROBE` executable paths take priority. There is no automatic apt/sudo or runtime package installation. Restart ComfyUI and refresh the webpage after installation/update.

## Models and upgrade compatibility

New source installations use:

```text
ComfyUI/models/kkagent/
  sensevoice/model.int8.onnx
  sensevoice/tokens.txt
  speaker-diarization/segmentation.onnx
  speaker-diarization/speaker_embedding.onnx
```

The installer verifies SHA256 for all assets, including the original segmentation archive before extracting its single model file. Downloads are only performed by the installation hook, never during node import or inference. Source release ZIPs do not include model weights. A separate `model.int8.onnx` attachment on the 0.2.19 release supplies the checksum-locked SenseVoice mirror; it is only needed if the original host is unavailable. Two historical speaker model files remain tracked in Git to avoid deleting existing installations' models on `git pull`; they are not included in the new source ZIP. They can be reused without downloading again.

Model directory priority: explicit `KKGO_ASR_MODEL_DIR` / `KKGO_SPEAKER_MODEL_DIR`, complete configured ComfyUI shared model folders, then complete legacy plugin-local `models/sensevoice` / `models/speaker-diarization` folders. A complete old installation continues using its existing models without moving, deleting or duplicating them. Partial legacy groups can be copied into one complete shared group; differing existing files are never silently overwritten.

For shared model stores, merge `extra_model_paths.yaml.example` into ComfyUI's `extra_model_paths.yaml` and edit `base_path`. The model categories are `kkagent_sensevoice` and `kkagent_speaker`. The actual selected speaker/ASR paths and media executable paths are passed into isolated media workers, so subprocesses use the same configuration.

Original model sources and licenses are recorded in `models/*/MODEL_SOURCES.json`, `scripts/model_assets.py` and `THIRD_PARTY_NOTICES.md`. They remain separate from the plugin's MIT license.

## Existing full offline bundles

Existing full offline releases remain available in Releases. For a complete bundle containing wheels, models, FFmpeg/FFprobe and `bundle-manifest.json`, use:

```bash
/path/to/comfy-venv/bin/python install.py --offline --comfy-root /path/to/ComfyUI
```

This explicit installation command verifies the bundle and installs the pinned `requirements-offline.txt` from bundled wheels with `--no-index`. No network is used. Older releases retain their original installer and instructions. Do not use `--offline` with a source-only ZIP or a clone. Ordinary source requirements list direct dependencies with compatible ranges; only the offline lock pins transitive dependency versions.

## Example and validation

Open `Workflow > Browse Templates` and select `KKagent_basic`, or drag `example_workflows/KKagent_basic.json` into ComfyUI. Upload/select a video, fill in the reverse-model and review-model keys, and replace the illustrative rules with your own. The example uses core loading, saving and preview nodes with the kk agent 2 entry, independent frame and reverse nodes. Video and IMAGE outputs may be connected to core save nodes. No credentials, personal reference images or user workflows are included.

Existing node IDs and VIDEO/context output positions are preserved. Historical frame links migrate to the independent frame node. The plugin now exposes a V3 `ComfyExtension` / `comfy_entrypoint`. `WEB_DIRECTORY` continues to serve the same frontend.

```bash
/path/to/comfy-venv/bin/python scripts/check_linux.py --comfy-root /path/to/ComfyUI
```

Validation and historical limitations are documented in `VALIDATION.md`. New installer checks use local fixtures; they do not establish remote model semantic quality or RunningHub deployment success. The 0.2.20 lifecycle suite covers prompt-end cleanup, cancellation and downstream failure handling.

## Registry metadata

`pyproject.toml` lists the same direct dependencies as `requirements.txt`, Linux/ComfyUI compatibility and the target `PublisherId = "tofu952711"`. The existing project identifier is preserved for compatibility. This does not register a publisher or publish a Registry package. A matching publisher account must be claimed/verified before a separate Registry publication. No Registry access token is stored or required for GitHub installation.

References: [Manager installation hooks](https://docs.comfy.org/custom-nodes/backend/manager), [Registry metadata](https://docs.comfy.org/registry/specifications), [V3 registration](https://docs.comfy.org/custom-nodes/v3_migration), [workflow templates](https://docs.comfy.org/custom-nodes/workflow_templates).

## 参考视频时长

32秒模式下，A链路及B链路每段输出的参考视频最长30秒；17秒模式下最长15秒。分析、抽帧、语音证据和反推提示词仍覆盖完整原片或该段完整分析区间，不移动原分段边界，也不将裁短的尾部并入下一段。

## 0.2.7：可编辑分镜参考帧

B链路实际分段完成后，每段使用一个独立抽帧AI会话。按用户要求、上传图片职责和真实候选画面，选择适合先修改图片、再交给视频模型参考的原视频帧。优先剪辑/转场结束后已经稳定、目标清晰的画面；模特、背景、产品都需要替换时优先清晰同框，无法同框时如实记录并使用必要的互补帧。

每段先分析镜头结构：全程画面基本不变、固定镜头只保留1张段首参考图；确认至少1次明显的整个画面/景别/场景突然转场剪辑时选2—5张，够用2张就选2张，不凑满5张。段首从开始找最早清晰且符合编辑要求的原帧，其他帧覆盖不同镜头的必要构图。所有图片遵守目标清晰、多目标优先同框、构图和接触关系完整、适合编辑等要求。导出的PNG保持视频实际像素分辨率，无缩放、拼图标签或生成内容；AI选帧拼图只是预览。每张图记录原片时间、本段时间、主体位置、所属段和选择原因。

各段分别生成视频，所以各段图片编号独立计算：原有上传图在前，抽帧图在后。例如上传image1—image3，第1段抽两张则使用@图片4、@图片5；第2段抽一张则重新使用@图片4。原有非连续插口保留插口编号，抽帧从最大已连接插口编号之后开始。用户修改这些帧后，应将修改后的图按本段编号放到参考图后面。子智能体收到的是本段完整映射，提示词在对应动作时间处引用修改后的图片。审核后程序再次校验引用，缺图或引用未提供的编号会报错。

- 一体节点 `KKGO_FullVideoAgent` 新增“分镜参考图列表”（IMAGE，列表输出）和“抽帧信息”（JSON文本）。
- 0.2.20起，分流工作流从 `KKGO_VideoFrames`（独立抽帧）取得提前输出的图片和抽帧信息；入口的video1—video5和“反推资料”原有位置不变。旧图片连线自动迁移。
- 将“分镜参考图列表”连接标准“保存图像”或其他支持IMAGE列表的节点，可一次取得全部抽帧。列表按段号、段内时间排序；用“抽帧信息”中的segment_index区分每次生成，output_list_index是整个列表中的1起算位置，segment_image_index是本段图序，image_number/mention是该段生成时的引用编号。
- 抽帧PNG保存在本次任务的 `b-results/<run_id>/frames/segment-N/`，对应记录见 `frame-manifest.json`，不会因临时媒体目录清理而丢失。
- 抽帧AI复用已有审核区域的接口、密钥和视觉模型，但每段是独立会话；每段一次视觉请求，契约校验失败时最多补正一次。候选覆盖全段、剪辑点附近，并加密段首1秒内逐帧观察，不能保证命中每个未检测到的瞬时镜头。
- A链路未新增分段抽帧，新增图片列表为空。RunningHub实机及真实远程模型选帧质量仍需验收；本次离线验证不代表语义选帧准确率。

## 0.2.8：按段独立取图

“独立反推”和一体节点在原有输出后增加“第1段参考图”至“第5段参考图”，每个都是IMAGE列表。第N段参考图只包含videoN对应段的抽帧，可以分别连接保存图像或图片编辑流程。原“分镜参考图列表”仍输出所有段的图片，原有连线和各段的图片引用编号保持不变。不存在的段停止该输出分支，不产生空白图，也不会让连接的保存节点报空列表错误。

## 0.2.9：每段3—5张与段首参考

每段至少3张、至多5张。抽帧AI必须从本段首个真实帧检查，清晰时第一张就用首帧；模糊、叠化或黑场时按时间选择段首附近最早清晰的真实帧，不调用图像修复、不生成画面。候选增加段首1秒逐帧采样，第一张之前的清晰度判断保存在opening_assessment，实际选中的段首锚点记录在opening_frame。不足3张或跳过已判断清晰的更早帧会触发一次AI补正，仍不符合则明确报错。清晰度由视觉AI判断，程序校验选择顺序与数量，不把像素锐度阈值当作语义清晰度。

## 0.2.10：抽帧图片提前输出

B入口先完成实际分段、共用规划及选帧，再返回视频和图片端口；此时反推子智能体和最终审核尚未开始。入口在原“反推资料”之后增加总图片列表、抽帧信息和第1—5段参考图；将保存/编辑图片节点接到入口即可先取得图片。独立反推节点随后恢复任务、生成并审核提示词。旧工作流重新加载时，连接到独立反推图片/抽帧信息端口的线会自动改接其上游入口对应端口，文本与视频连线不变。一体节点仍会等整条链路结束，如需提前取图请使用分流入口。

## 0.2.11：每段固定两张参考图

替代0.2.9的3—5张要求。每段只输出两张：段首参考与自然内容节点参考。两张都复用清晰、多目标优先同框、方便修改、构图/持握关系完整等要求；首帧符合就保留，不符合则从段首附近选最早合适的真实帧。第二张优先转场/剪辑结束后稳定画面，或人物说完一句话后的自然停顿，不按50%时间点机械抽取。抽帧AI新增接收本段带时间的语音转写，与候选画面联合判断，不将ASR窗口边界直接认定为句末。没有合适转场/句末时如实选有补充价值的清晰原帧。反推用的0.25秒密集预处理采样、图片编号、分段输出与提前返回时机不变。

## 0.2.12：按镜头结构决定参考图数量

替代固定2张：每段独立判断，固定镜头或切换很少（确认0—1次切镜）保留2张；切换较多（确认至少2次切镜，即至少3个镜头）选择3—5张。抽帧AI收到本段像素切点候选、真实画面和转写，排除人物动作、连续运镜、字幕变化等假切点，并为确认切镜提供相邻候选帧的前后对照。程序检查依据字段、模式与数量是否一致，不把候选数直接当作真实切镜数。shot_analysis随抽帧信息输出并传给反推子智能体。频繁切镜优先覆盖不同镜头必要构图，3张够用就不增加；段首要求、编辑适用性、原像素尺寸、独立编号及提前图片输出不变。

## 0.2.13：两张模式的第二张靠近中间

仅调整2张模式：第一张段首规则不变，第二张优先在本段35%—65%范围搜索符合原有清晰度/编辑适用性/同框/接触关系要求的原帧。在该范围中优先稳定转场或说完话后的自然停顿，不强制精确50%。范围内没有合适帧才向两侧扩大，并说明原因。候选补充时间中点附近画面和segment_position相对位置，输出reference_role标注“中间附近”。3—5张模式仍按镜头覆盖选图，预处理密集采样、分辨率与提前输出保持不变。

## 0.2.14：固定画面仅段首一张，明显剪辑3—5张

替代此前2张规则。每段全程基本不变、固定镜头时仅1张段首参考图；至少1次明确的整个画面/景别/场景突然剪辑或转场就选3—5张。人物动作、产品移动、字幕和贴纸变化仍不直接算剪辑，抽帧AI须给出前后相邻候选画面的真实变化依据。程序校验fixed模式无确认切镜且仅1张，edited模式有确认切镜且3—5张，不接受2张。第一张继续从段首检查清晰度、目标可见性和编辑适用性，不合适才选附近最早合适的原帧；其他图优先稳定转场后的必要构图，清晰同框等原约束不变。取消中间帧专用选取，原有预处理、原分辨率导出、按段编号与提前图片输出保持不变。以上历史版本小节仅供变更记录。

## 0.2.15：剪辑画面改为2—5张

固定画面仍仅1张段首参考。出现明显突然转场或剪辑时，数量从3—5张改为2—5张，够用2张就不额外增加，最多5张。段首、清晰度、编辑适用性、同框、分段编号和提前输出等约束保持不变。

## 0.2.16：场景未变优先仅取段首

每段先检查全段候选，首帧之后场景一直未变时，默认只输出1张段首参考图。同场景内的人物动作、产品移动、跳剪和景别切换不增加参考图，此规则优先于此前按剪辑数量选图的规则。首帧不清晰或不适合编辑时，仍取段首附近最早合适的真实帧。只有场景明确切换并提供相邻候选帧前后依据时，才输出2—5张。`shot_analysis.scene_unchanged`记录场景判断，程序检查模式、依据与图片数量一致性；场景是否相同仍由视觉AI判断。

## 0.2.17：反推子智能体空正文重试

B链路反推子智能体收到无工具调用且正文为空的正常接口响应时，额外重试最多3次，即首次加3次重试共最多4次。每次原样保留系统规则、用户要求、参考图、证据和已经完成的工具上下文，不追加空回复、不重跑其他已完成分段；重试间隔1/2/4秒。各次用量均累计，空回复在速度记录中标为`empty_response`，控制台显示重试次数。重试仍受本任务剩余超时和取消控制；3次后仍为空则明确报错。现有HTTP错误重试和图片引用补正规则继续保留。

## 0.2.19: frame export boundary tolerance

Selected reference frames may fall within 0.5 seconds of the recorded segment boundary without failing export. The candidate-to-source-frame PTS check remains unchanged: no nearest-frame substitution, time shifting, resampling or trimming changes. This fixes false rejection when a segment start rounded to six decimals is slightly later than the same decoded frame's PTS.
