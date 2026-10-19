# 旧格式兼容夹具

这些人工合成夹具由本轮重构前的完整源码副本生成，包含 JSON v4 索引、SQLite 会话/轨迹表，以及序列化类路径为 `deepsearch_agent.chunking.TextChunk` 的 LangGraph 检查点。SQL 文件是 SQLite `iterdump` 输出，测试时仅在临时目录恢复；不包含实际用户资料。轨迹没有 `run.classified`，用于验证旧事件仍可重新分类。

夹具生成后不随当前实现重写，以固定跨版本读取和 ID/哈希校验边界。
