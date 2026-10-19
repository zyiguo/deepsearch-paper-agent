# MiMo-V2.6-Pro 配置

核对日期：2026-10-06。主模型与独立视觉模型均可使用 `mimo-v2.6-pro`。

## 官方规格与运行预算

| 项目 | 官方上限 | 当前本地运行配置 |
| --- | --- | --- |
| 上下文 | 1,048,576 Token | 1,048,576 |
| 单次生成 | 131,072 Token | 32,768 |
| 请求读取超时 | 不是模型规格 | 300 秒 |

来源：[官方模型页](https://mimo.mi.com/models/en-US/mimo-v2.6-pro)、[官方集成配置中的精确限额与图像输入声明](https://mimo.mi.com/docs/zh-CN/tokenplan/integration/opencode)、[Chat API](https://mimo.mi.com/docs/en-US/api/chat)。

32K 是本项目选择的运行预算，不是模型能力上限。需要更长输出时，可将 `LLM_MAX_OUTPUT_TOKENS` 与 `VISION_MAX_OUTPUT_TOKENS` 提高到不超过 131072；上限不代表每次一定产生这些 Token。更大的实际输入和输出会增加费用、耗时；当前非流式模型请求仍可能先触发 300 秒读取超时。

`.env` 使用 `LLM_MODEL`、`LLM_CONTEXT_WINDOW`、`LLM_MAX_OUTPUT_TOKENS`、`LLM_TIMEOUT_SECONDS`；`.env.vision` 使用对应 `VISION_` 变量。公开模板仅提供无密钥的配置示例，实际文件保留本机密钥与接口地址，不提交版本库。官方按量 API 基础地址为 `https://api.xiaomimimo.com/v1`；第三方网关可能有更低限额，不能仅凭模型名称推断。

## 请求与上下文

- 网关沿用 MiMo 的 `max_completion_tokens`，不发送 `max_tokens`。该预算包含可见输出与推理 Token。
- 结构化研究与视觉任务保持 `thinking: disabled` 和 JSON 输出约束；本次不改变推理模式，也不放宽 schema 与引用检查。
- 主模型、视觉预算各自加载，重排与研究上下文继续保留输出空间；没有将整个窗口当作净输入额度。
- 1M 是窗口容量，不是每次强制填满。top-k、工具次数、证据数、图片数量、工具观察长度和安全限制保持原样；Token 计数仍是本地估算，并非 MiMo 官方 tokenizer 的精确计数。
- 模型配置在应用启动时加载，修改后需重启后端；不自动中断运行中的研究请求。

## 评测边界

消融脚本现在默认继承 `VISION_MAX_OUTPUT_TOKENS`，包括预处理 OCR、在线 OCR 和工具探测；`--max-output-tokens` 可显式覆盖。模型、上下文和输出预算继续写入 run identity，防止旧结果与新配置混合续跑。

原始实验与发布审计中的历史模型和预算不改写。切换后应使用新的评测目录；要比较代码修复效果，需控制模型和预算一致，不能将换模型收益归因于代码修复。初次切换只做离线验证；后续小样本真实调用与配对结果见 [回归报告](PAIRED_REGRESSION_20261006.md)，未实测 1M 长上下文质量。
