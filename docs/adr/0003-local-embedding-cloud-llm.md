# ADR-0003：本地 Embedding 与云端 LLM 分离

## 状态

Accepted

## 背景

用户希望获得云端模型的回答质量，但不希望为了建立索引而上传全部 PDF 文本，并希望控制持续 Embedding 成本。

## 决策

使用本地 BGE-M3 生成 1024 维 dense embedding。云端 OpenAI-compatible LLM 只接收问题、必要会话上下文和检索出的少量证据片段。

## 后果

- 优点：全库文本不因索引而发送到云端；Embedding 成本可控；服务商可替换。
- 缺点：需要本地模型缓存和 GPU/CPU 运行管理；检索出的证据片段仍会发送至云端。
- 替代方案：云端 Embedding 更简单，但会上传全部 Chunk；完全本地 LLM 的质量和硬件要求不符合当前选择。
