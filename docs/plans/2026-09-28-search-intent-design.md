# 自训练模型：首轮检索决策接入

## 范围

使用后训练项目的 search/answer JSON 协议，作为可选的首轮检索决策模块。保留 Supervisor、后续 Researcher 补查、Answerer、Verifier、文献范围过滤及原文引用链路。暂不配置云端，不安装模型、不加载权重、不发送真实请求。

第一版只替代首轮 Researcher 的查询生成，不做闲聊/比较/总结等多类别意图识别，也不控制多轮搜索停止。当前论文问答必须寻找证据，因此 answer（包括 UNKNOWN）只代表模型建议，实际回退原 Researcher。模型生成的答案直接丢弃。

## 本地接口

`SearchIntent(client)` 要求 client 提供 `complete_messages(messages) -> str`。系统提示沿用训练协议；最近历史带有“非证据”标签且限制长度。只接受一个 search 或 answer 键，拒绝重复键、空值、超长值及附加文本。

通过 `AnswerService(..., use_graph=True, search_intent=policy)` 注入。默认 None，现有 Web 服务行为不变；非图问答路径不使用此模块。未来配置云端时再添加服务生命周期与独立配置，不替换主模型。

```python
from deepsearch_agent.search_intent import SearchIntent

class LocalStub:
    def complete_messages(self, messages):
        return '{"search":"RAG 检索器训练方法"}'

policy = SearchIntent(LocalStub())
print(policy.decide('RAG 的检索器如何训练？'))
# AnswerService(main_client, kb, use_graph=True, search_intent=policy)
```

## 回退和验证

- search：走已有检索和重排流程，保留原文及页码。
- answer、UNKNOWN、非法 JSON、ModelServiceError：走原 Researcher 查询生成。
- 不捕获任意编程异常；服务客户端负责将网络失败转换为 ModelServiceError。
- 后续补查继续由原 Researcher 处理，保持既有轮数限制。
- 测试使用模拟客户端/HTTP transport，覆盖首轮路由、补查、核验、非法协议与故障回退。

PowerShell 自验：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_search_intent.py tests/test_research_graph.py -q
```

这只是接口与控制流验证，不代表训练模型在论文检索上已有收益。后续接入真实检查点后，再比较原 Researcher 与该模块在相同问题/语料下的检索召回、误判和延迟。
