# PDFVQA / ChartQA 评测设计

用户要求：先查 GitHub 参考实现，搭建解耦的公开数据集评测；小样本验收后，各跑约 100 题。

## 参考与边界

- 官方数据：adlnlp/pdfvqa、vis-nlp/ChartQA。
- 参考 VisRAG 的检索/生成分离，Pix2Struct / VLMEvalKit 的 ChartQA relaxed accuracy。
- ChartQA visual RAG 项目指出原问题有图像指代依赖。不改写原问题、不将受控干扰图实验冒称官方开放检索榜单。
- PDFVQA A 是固定答案分类；B/C 是元素选择。原版 CSV/PKL 和竞赛版本不可混用：官方教程的验证 CSV 无答案，必须拒绝评分。
- 可安全读取公开 PDFVQA Hugging Face 镜像的页面 JSON，明确记录镜像来源和版本；A/B 单页数据不能冒充 C 的完整文档任务。B 的文字答案只在唯一匹配元素时映射为元素 ID，歧义/缺失单独报告，不猜测。

## 模块

1. 数据规范化：独立 manifest；原图、本地校验和、固定抽样 seed、split、任务、题目、金标准；路径受根目录约束。答案/关系标注绝不写入检索内容或模型输入。
2. 离线指标：ChartQA 数值相对误差 5%、非数值大小写忽略精确匹配；PDF A accuracy、B/C 元素集合 exact match 和诊断 F1；检索与重排以原页为单位 Recall/MRR/nDCG。
3. 运行器：oracle/given-context、固定干扰页 rerank、真实索引 RAG。独立输出，保留原输出、错误、延迟、统一证据 ID。错误计零分，不从分母删除。
4. 项目适配器：复用 KnowledgeBase、retrieve、MultimodalReranker、VisualEvidence；结构化基准生成，不假称覆盖 LangGraph、skills 和记忆工作流。
5. 版本化断点续跑：manifest + 图像校验和 + 代码 + 配置指纹一致才续跑；远程运行需显式开关；并发有界。公共评测数据与用户知识库分离。

## 第一轮

- ChartQA validation：固定 seed，human/augmented 尽量等额，共 100 题。
- PDFVQA validation A/B：固定 seed，A/B 尽量等额，共 100 题，报告独立页数和歧义排除数。
- 先小样本通过格式、评分和原图读取验收，再执行 100 题给定上下文基线。检索/重排配置独立执行，不把阅读基线结果当作 RAG 成绩。
