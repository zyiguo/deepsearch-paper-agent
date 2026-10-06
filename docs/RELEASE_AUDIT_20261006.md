# Badcase 修复与发布审计

日期：2026-10-06。审计者：Codex。类型：代码检查、自动化测试及依赖扫描；不是独立渗透测试或人工专家质量验收。

## 修复依据

原始实验仍保留在 [消融报告](AGENTIC_ABLATION_RESULTS.md) 和 [抽查记录](AGENTIC_ABLATION_AUDIT.md)。
本轮没有覆盖旧预测、改写金标或给格式无效答案补分。

| 观察到的问题 | 本轮调整 | 验证边界 |
| --- | --- | --- |
| 格式失败样本中，Supervisor 缺必填字段、额外字段或非法结构 | 保留严格 schema；每个工作流实例最多一次格式修复请求，错误定位不包含输入秘密 | 脚本模型测试通过；不是实际减少了 15 条格式失败的声明 |
| PDF 元素 ID 与证据引用编号混淆 | 提示明确区分；越界编号在调用 Verifier 前拦截，优先重新起草，不为编号错误重复检索 | 保留引用安全校验；三轮修订仍无法修复则拒绝输出未支持结论 |
| OCR 预算耗尽后仍反复调用 | 从后续可选目录移除耗尽 OCR，并在执行前再次拦截 | 限制仍有效，不以无限增大预算代替修复 |
| 页级问答被扩展为整篇文献问题，元素选择要求被误解为长答案完整性 | 所有角色明确给定页面范围；Answerer/Verifier 使用任务专用约束，只选择直接回答问题的元素 | 不保证自动归因已经成为因果结论，需新一轮配对实测 |

## Token 预算

- 本地主模型：最大输出 1024 -> 4096，上下文显式设为 32768。
- 消融脚本：默认最大输出 2048 -> 4096，新增 `--max-output-tokens` 可覆盖；预算写入新的 run identity。
- 视觉模块原有 8192 输出预算保持不变；单题最多 18 次模型请求的实验限制保持不变。
- 原始正式实验仍是 2048 输出配置；新配置必须使用新的输出目录，不能混进旧实验。

## 依赖审计

首次 Python 环境扫描报告三个包存在已知问题：pip 24.2、pypdf 6.16.2、urllib3 2.7.0。
已从 PyPI 官方 HTTPS 源更新到 pip 26.2.1、pypdf 6.19.0、urllib3 2.8.0。
`pyproject.toml` 提升 pypdf 安全下限，`constraints-security.txt` 约束安装时的安全版本范围。
它不是完整依赖锁文件，后续仍需重新扫描。

修复依据：[pypdf 6.19.0 发布记录](https://github.com/py-pdf/pypdf/releases/tag/6.19.0)、[urllib3 2.8.0 发布记录](https://github.com/urllib3/urllib3/releases/tag/2.8.0)。
升级后 `pip-audit --path .venv/Lib/site-packages` 未报告已知漏洞，`pip check` 无依赖冲突。
前端完整扫描另发现测试依赖与 source-map-js 的四项漏洞：升级 Vitest 至 5.0.3、source-map-js 至 1.2.2，旧 tinypool 随依赖更新移除。
使用 Node 24.5.0 执行干净的 `npm ci`，随后完整 `npm audit --audit-level=low` 报告 0 个已知漏洞，包括开发依赖。
修复依据：[Vitest mocker 公告](https://github.com/advisories/GHSA-82fw-gwwq-j7x9)、[tinypool 公告](https://github.com/advisories/GHSA-85c8-ppgw-ccpr)、[source-map-js 公告](https://github.com/advisories/GHSA-68fv-2mgg-jv7q)。
这是扫描时点及该数据库覆盖内的结果，不代表没有未知漏洞。

## 最终回归

- 后端：`pytest -q -rs --basetemp tmp/publish-final2-20261006`，534 项通过、2 项跳过。
- 前端：Vitest 5.0.3 下 10 个文件、57 项测试通过；TypeScript 与 Vite 生产构建通过。
- 发布扫描仅对 `tests/test_mcp_runtime.py` 中固定的 localhost 假凭据拒绝测试设置精确例外；其他文件、主机或凭据均仍被拦截，已有回归测试。

## 发布边界

- 包含本轮修复以及配套的多模态检索、MCP、动态技能、工作记忆、评测、Wiki 模块和测试文档。
- 排除 `.env` 实际配置、密钥、`data/` 原始数据和轨迹、个人会话、工作记忆、生成的 Wiki、缓存与个人笔记。
- `scripts/audit_release.py` 检查 Git index 中的运行数据目录、私密环境文件、大文件与常见密钥模式；只输出路径/行号，不回显疑似秘密。扫描规则不是秘密检测的完整证明，也不扫描全部 Git 历史。
- 已审查 MCP 工具白名单与本地范围约束、动态技能只读加载、工作记忆与 Wiki 的来源隔离。未加入公网鉴权：仍只能作为本地单用户应用运行。
- 两个符号链接测试因当前 Windows 账户无法创建符号链接而跳过；不将跳过项记作通过。

## Wiki 设计

GitHub 项目对照、实现范围和未实现能力见 [LLM_WIKI.md](LLM_WIKI.md)。
采用独立 Markdown 派生知识层，复用现有检索与核验；不安装额外的 Kiro 或第二套编译服务。
未通过核验的生成结果不会保存，Agent 读取 Wiki 后仍必须回到原始证据。

## 质量结论

本轮是工程修复及离线回归，不是新的云端消融实验。不能据此宣称答案准确率、OCR 准确率或工具选择质量提升。
原实验的图像 embedding HTTP 402 限制仍未解决，直接图像向量检索的真实效果没有在本轮重新验证。
