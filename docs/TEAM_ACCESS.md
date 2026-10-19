# 团队账号与工作空间

这是可选的小团队模式，仍为单 API 进程。默认不启用账号，既有单用户接口和本地文件保持可用。团队模式将业务资源放入独立的新工作空间，不会把原有文献、会话或轨迹自动共享给新成员。

启用 [PostgreSQL](DATABASE.md) 时，身份、成员、会话/记忆与运行记录改由业务数据库保存，下述 SQLite 路径只用于默认本地后端。原文、索引、Wiki、工作记忆文件、检查点和轨迹仍位于各空间目录；各空间共享一个数据库连接池，业务行由空间键和强制 RLS 隔离。

## 配置与首次启动

如果还没有 `.env.access`，从示例创建后编辑；已有文件请直接编辑，避免覆盖：

```powershell
Copy-Item .env.access.example .env.access
.venv/Scripts/python.exe -m deepsearch_agent.application.access_cli create-user --username admin --display-name 管理员 --admin
```

CLI 会交互询问并确认密码，长度 12–128 个字符；密码不通过参数传入，不生成默认账号。忘记密码时，本机操作者可运行 `reset-password --username 用户名`，同样通过密码提示输入；会撤销该账号全部会话。

已经配置 PostgreSQL 时，先按数据库说明执行 schema upgrade，再创建新管理员。迁移旧团队则先导入身份库，避免新账号使目标身份库不再为空。

本机 HTTP 开发先在 `.env.access` 中设置 `ACCESS_COOKIE_SECURE=false`；对外 HTTPS 部署保持 `true`，并将 `ACCESS_ORIGINS` 设置为实际工作台的精确 Origin。文件值覆盖同名环境变量；相对资源根目录从项目根解析，不依赖启动工作目录。

```powershell
.venv/Scripts/python.exe -m uvicorn deepsearch_agent.api:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers --log-config src/deepsearch_agent/logging.json
```

前端按原 README 启动。登录后先创建工作空间，再导入资料。系统管理员可在“空间与账号”创建、停用或恢复普通账号；用户可修改自己的密码。空间所有者按已有用户名添加成员，选择只读或可编辑。恢复账号不会恢复已撤销的旧登录。

| 配置 | 默认值 | 说明 |
|---|---|---|
| `ACCESS_MODE` | `local` | `local` 或 `team`；未知值拒绝启动 |
| `ACCESS_DATA_ROOT` | `data/team` | 新身份库与工作空间目录；不复用旧单用户数据路径 |
| `ACCESS_COOKIE_SECURE` | `true` | HTTPS Cookie；仅本机 HTTP 开发可设 false |
| `ACCESS_SESSION_SECONDS` | `28800` | 固定会话期限；允许 300–86400 秒 |
| `ACCESS_MAX_OPEN_WORKSPACES` | `8` | 单进程保留的运行环境数，允许 1–32；不是任务并发数 |
| `ACCESS_ORIGINS` | 本机 5173/8000 的精确 Origin | 逗号分隔，无尾斜杠、通配符或路径 |

团队模式要求关闭 MCP 客户端与服务端配置，否则启动明确失败；避免未授权的第二个资源入口。本轮没有实现空间所有者转移或删除空间。

## 权限和数据边界

| 操作 | owner | editor | viewer | 系统管理员但未加入空间 |
|---|---|---|---|---|
| 查看空间文献、原文、附件、历史、记忆、Wiki、运行状态 | 是 | 是 | 是 | 否 |
| 上传、入库、发起研究、取消/恢复、修改或删除业务内容 | 是 | 是 | 否 | 否 |
| 添加/修改/移除成员 | 是 | 否 | 否 | 否 |
| 管理系统账号 | 取决于是否系统管理员 | 同左 | 同左 | 是 |

权限单位是整个工作空间；空间内成员按角色共享全部业务数据。本轮没有空间内按单篇文献或单个会话设置私人权限，也没有公开分享链接。所有者不能被降权或移除；普通成员不能将自己或他人提升为所有者。

原业务路径在团队模式加前缀，例如 `/w/{workspace_id}/api/sessions`。无前缀的旧业务路径拒绝访问；前端统一给 JSON、SSE、图片和下载链接加前缀，切换空间后完整导航，清除旧页面内存状态。不同空间的会话选择 ID 使用不同本地存储键。后端每次请求查询当前账号与成员关系，不依赖前端隐藏按钮。

所有修改请求额外携带 `X-DeepSearch-Request: 1`，包括登录和流式 POST。跨站来源拒绝；非浏览器客户端可不带 Origin，但必须带上述头并使用 Cookie jar。只读成员不能发起 POST 问答，因为研究会消耗资源并保存记录。

```text
ACCESS_DATA_ROOT/
  identity.sqlite3                 # 用户、会话摘要、成员关系、安全事件
  workspaces/<server-generated-id>/
    index.json / embedding-*.json
    conversations.sqlite3
    research_runs.sqlite3
    research_checkpoints.sqlite3
    conversations.working-memory/
    documents/                     # 原件、解析缓存、附件
    wiki/
    traces.sqlite3                 # 仅显式启用轨迹时
```

配置了 `RAG_TRACE_DB` 或装配时传入轨迹路径，团队模式会将记录固定到各空间 `traces.sqlite3`，不会写入共享全局轨迹文件。轨迹元数据包含 `workspace_id`；模型结果、内部核验与人工标签仍分离。HTTP 没有直接下载轨迹数据库的接口；本机导出 CLI 仍属于拥有磁盘访问权限的管理员工具，使用时须选择正确空间和审核数据接收方。

Redis namespace 由部署身份与空间 ID 组合派生，不改变原有模型/凭据缓存身份。共享账号配置属于服务器操作者，本轮没有按成员上传任意模型密钥的接口。

## 容器配置

在本机私有 Compose 覆盖配置中为 `api.environment` 注入以下字段即可启用；无需将 `.env.access` 或用户资料加入构建上下文：

```yaml
services:
  api:
    environment:
      ACCESS_MODE: team
      ACCESS_DATA_ROOT: /app/data/team
      ACCESS_COOKIE_SECURE: "true"
      ACCESS_ORIGINS: https://research.example.com
```

替换示例 Origin，并确保浏览器通过同一受控 HTTPS 入口访问前端、`/api/` 和 `/w/`。单机回环 HTTP 验证使用 false。首次容器管理员需在该容器/卷内执行上述密码提示 CLI，例如 `docker compose exec api python -m deepsearch_agent.application.access_cli create-user --username admin --admin`，不能在宿主机另一份身份库中创建后假定容器自动读取。

`/api/health/live` 与 `/api/health/ready` 对外仅报告团队控制入口可用，不声明所有空间索引或模型可用。空间成员可访问 `/w/{id}/api/health/ready` 检查该空间 Redis/索引状态。运行环境按需创建并在退出时依次关闭，先取消并等待任务，再关闭模型、PDF、检查点和 Redis 资源。达到已打开空间容量时返回 503，需由操作者评估容量或安排重启；不会自动驱逐活跃任务。

## 验证与运行限制

回归命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_team_access.py tests/test_architecture.py -q
.venv/Scripts/python.exe -m deepsearch_experiments.architecture
```

真实验证结果见 [本轮验证记录](TEAM_ACCESS_VALIDATION_20261018.md)。退出、停用、成员撤销作用于后续请求；已接纳的研究及已经打开的 SSE/文件响应可能继续完成。旧密码重置与新会话签发有事务检查，避免重置后出现使用旧凭据新签发的会话。

本轮不提供持久任务队列、用户费用额度、自动备份恢复、多副本、SSO/MFA、邮箱找回、反向代理 TLS 配置或外部模型效果验收。团队试用前应完成真实账号初始化与 HTTPS 配置；现有本地资料需要未来显式导入流程，不能直接将旧全局目录挂给所有空间。
