export function modelMode(mode: string | null) {
  if (mode === 'live-llm-live-retrieval') return {
    label: '真实模型 · 真实检索', tag: 'LIVE', description: '真实模型生成 · 真实语义检索 · 支持纯文本 PDF',
    detail: '围绕已入库文献自由提问，点击证据页码查看原文。问题、历史和检索片段会发送至配置的模型服务并可能计费。返回片段不等于存在答案，请核对证据。',
  }
  if (mode === 'live-retrieval-demo-llm') return {
    label: '真实检索 · 模拟回答', tag: 'MIXED', description: '真实语义向量 · 固定回答 · 支持纯文本 PDF',
    detail: '可自由提问以检验检索，但生成端仍是固定演示回答。向量化会调用配置的 Embedding 服务。',
  }
  if (mode === 'demo') return {
    label: '演示模式', tag: 'DEMO', description: '模拟论文 · 固定向量 · 固定回答',
    detail: '当前仅支持两个示例问题，不代表真实模型效果。',
  }
  if (mode === 'live-llm-demo-retrieval' || mode === 'qwen-demo-retrieval') return {
    label: '真实模型 · 演示检索', tag: 'LIVE', description: '真实模型生成 · 模拟论文 · 固定向量',
    detail: '仍仅支持两个示例问题。问题、历史和证据会发送至已配置的模型服务，可能产生费用；此状态不代表模型连通性已验证。',
  }
  return {
    label: '模型模式待确认', tag: 'INFO', description: '尚未确认当前模型模式',
    detail: '请刷新文献列表与状态。历史回答不会被重新标记为当前模型生成。',
  }
}
