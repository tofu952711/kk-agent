# 节点中的 AI Base URL

`KKGO_FullVideoAgent` 和 `KKGO_VideoSegments` 均提供原生字符串输入：

- `base_url`：显示为“反推 AI Base URL”，默认 `https://api.deepseek.com`，供反推及 B 分段模型请求使用。
- `review_base_url`：显示为“审核 AI Base URL”，默认 `https://api.zdjapi.com`，供审核及 B 抽帧模型请求使用。

填写的地址实际传入对应请求，不再把反推地址覆盖为固定值。旧 API 工作流未提供地址时沿用原默认值。`model`显示为“反推 AI 模型名称”，`review_model`显示为“审核 AI 模型名称”，填写值用于实际调用；接口协议保持原配置。

原生输入顺序为“密钥 → 反推 AI Base URL → 反推 AI 模型名称 → 本次用户要求”。旧工作流加载时按新顺序迁移，保留密钥、规则、地址及其连线和时长模式的值。本次不增加侧栏或其他前端插件界面，RH 可直接使用原生节点输入和 API 字段。

已通过两种入口及 A/B 路径的地址传递检查、6 项时长/真实执行器回归检查，以及旧工作流迁移、地址标签和保存重载的 JavaScript 检查。验证未调用付费模型。
