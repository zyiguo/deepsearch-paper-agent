# 图表与扫描页配置

项目支持 OpenAI 兼容的云端视觉模型。服务必须支持 Chat Completions 中的 PNG `image_url` 输入和结构化 JSON 输出。

在项目根目录 `.env.vision` 配置下列字段，可参考 `.env.vision.example`：

```ini
VISION_BACKEND=openai_compatible
VISION_API_KEY=your-key
VISION_BASE_URL=https://your-provider.example/v1
VISION_MODEL=your-vision-model
VISION_TIMEOUT_SECONDS=120
VISION_MAX_OUTPUT_TOKENS=8192
VISION_CONTEXT_WINDOW=32768
VISION_MAX_IMAGES=8
VISION_IMAGE_TOKEN_BUDGET=2500
```

视觉模型独立于 `.env` 的对话模型和 `.env.embedding` 的向量模型。接口地址填写基础地址，不带 `/chat/completions`。参数必须匹配所选模型的上下文与输出限制。图片 Token 数是估算，按服务商说明调整。

使用同一家服务时，可设置 `VISION_USE_LLM_CREDENTIALS=true`，省略 `VISION_API_KEY` 和 `VISION_BASE_URL`。应用启动时注入主模型的密钥与地址，视觉模块保留独立模型名和参数，不复制密钥。当前 MiMo 配置使用 `VISION_MODEL=mimo-v2.5`，文字模型继续使用 `mimo-v2.5-pro`。

重启后端，`/api/health` 的 `vision_enabled` 应为 `true`。仍需真实 Embedding 配置才能上传文献。新上传 PDF 会逐页识别，状态依次为排队、解析、识别、生成向量、可提问。已有文字文献点击“识别图表与扫描页”升级，或调用 `POST /api/documents/{id}/analyze`。

可以提问“表 1 中不同方法的误差分别是多少？”或“图 3 的横纵坐标是什么，曲线表现出什么变化？”。视觉证据来源中可查看对应原页。扫描页的 OCR 文字也可以检索。

每页首次分析会调用视觉服务；问答和核验也可能调用。接口失败后点击重新处理；已成功识别的页面通过本地缓存复用。更换视觉模型后需要重新分析已有文献。默认关闭视觉功能时，已入库的视觉证据保留，但涉及这些证据的问答需要重新开启视觉模型。

## 直接图像向量检索

### 独立图片与提问附件

资料库支持上传 PNG、JPEG、WebP，作为单页资料进入识别、文字索引和图像索引流程，可与 PDF 一起限定检索范围。

会话输入区支持上传或粘贴图片，每次最多附带 4 张。未选择资料时直接围绕附件回答；选择资料时，视觉模型读取附件生成检索问题，再结合资料证据回答和核验。附件随会话保存，可移除、重新用于提问，或通过“加入资料库”转为长期检索资料。当前附件用于视觉理解和生成文字检索问题，并非直接用附件向量执行以图搜图。

单图限制 10 MiB、2000 万像素；仅接受静态图片，统一校正方向、去除元数据并转为 PNG，最长边不超过 3200 像素。仅上传会话附件不会调用云端模型；发送问题或加入资料库时才可能产生模型费用。删除会话会清理其附件，已单独入库的副本不受影响。附件按会话隔离，这不是多用户身份认证机制。

接口：`POST /api/documents/upload-image?filename=chart.png` 入库；`POST /api/sessions/{id}/attachments?filename=chart.png` 上传附件，均使用图片二进制请求体和对应 Content-Type。提问接口增加 `attachment_ids`；仅附件提问传 `document_ids: []`。`POST /api/sessions/{id}/attachments/{attachment_id}/index` 将附件入库。

另配置 `.env.image_embedding`，可参考 `.env.image_embedding.example`：

```ini
IMAGE_EMBEDDING_BACKEND=siliconflow
IMAGE_EMBEDDING_USE_EMBEDDING_CREDENTIALS=true
IMAGE_EMBEDDING_MODEL=Qwen/Qwen3-VL-Embedding-8B
IMAGE_EMBEDDING_DIMENSIONS=1024
IMAGE_EMBEDDING_BATCH_SIZE=1
```

当前适配 SiliconFlow 的视觉 Embedding 接口；共享标志只注入 `.env.embedding` 的密钥和基础地址，视觉向量模型独立选择。共享地址必须支持该视觉模型；使用其他账号时改为 `false` 并填写 `IMAGE_EMBEDDING_API_KEY`、`IMAGE_EMBEDDING_BASE_URL`。模型输入格式见 [SiliconFlow 官方接口](https://api-docs.siliconflow.cn/docs/api/embeddings-post)。

重启后 `/api/health` 的 `image_retrieval_enabled` 应为 `true`。新上传 PDF 自动将各页原图向量化；旧文献点击“建立图像索引”，或调用 `POST /api/documents/{id}/index-images`，只补建图片向量，不重新运行 OCR。已完成的索引不重复计费；发生整批失败时旧快照不替换，重试可能重新生成该文献的图像向量。

在线检索在已选文献范围内分别运行文字混合检索与原图检索。图像分支用同一视觉 Embedding 模型编码问题，两路结果通过 RRF 排名融合，不混合不同向量空间的余弦分数。含直接图片命中的重排使用视觉模型；超出视觉预算或重排失败时保留融合顺序。随后回答和核验读取实际原页，返回 `evidence_kind=image` 与页码引用。

图像索引不要求页面存在 OCR 或图表描述。关闭视觉识别仍可入库纯扫描页的图片向量，但回答这些页需要启用视觉模型。图像向量与文字向量在同一原子 JSON 快照中保存，模型身份独立校验；关闭图像检索时保留已有图像数据，不再调用视觉 Embedding。更换图像 Embedding 模型会阻止不兼容快照加载，需保留旧快照后重建，不能跨模型复用向量。

当前实现是整页单向量检索，不是 ColPali/ColQwen 的多向量 late interaction，也未实现图表区域框选。复杂表格、小字号、模糊扫描和密集公式可能识别错误，关键数值请核对原页。首次验收建议各选一份扫描论文和图表论文，分别核对 OCR、图像召回、答案与引用位置。两张合成曲线的真实云端检索冒烟验证不代表真实论文集的召回率或准确率。

依赖接口与数据流见 [多模态设计](plans/2026-10-02-multimodal-design.md)。图片消息格式参考 [OpenAI 视觉输入文档](https://developers.openai.com/api/docs/guides/images-vision)。
