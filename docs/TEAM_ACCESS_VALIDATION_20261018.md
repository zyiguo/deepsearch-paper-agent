# 团队身份与空间隔离验证（2026-10-18）

本记录补充先前架构与容器验收。业务数据和账号均为合成夹具；没有使用真实用户账号或发起付费云模型调用，没有启用或改写本机 `.env.access`。

后续 PostgreSQL 接入的最新回归和运行边界见 [数据库验收记录](DATABASE_VALIDATION_20261018.md)；下述数字保留团队权限阶段的验收结果。

## 验证结果

| 检查 | 观测结果 |
|---|---|
| 后端完整回归，连接真实 Redis | **803 passed, 2 skipped in 87.95s**；两项跳过仍是 Windows 符号链接权限限制 |
| 团队权限专项 | **12 passed**：密码盐/校验、会话摘要/过期/轮换/撤销、并发登录限频、默认拒绝/来源校验/密码不回显、跨空间隔离、角色更新/撤销、账号停用/恢复、密码变更并发、运行环境异常关闭/容量、Cookie、本地兼容、真实 PDF/附件/Wiki/中断任务重启读取 |
| 专项最终复查 | 团队权限、架构与容器清理安全共 **36 passed** |
| 前端 | **11 个文件、62 passed**；TypeScript 与 Vite 构建成功，62 个模块 |
| 依赖边界 | **177 个 Python 模块、407 条边、0 循环、0 违规边**；不放宽原审计规则 |
| 原单用户接口 | 原冻结 OpenAPI 兼容测试通过；模式发现接口隐藏于原 OpenAPI，不删除旧路由或修改业务响应结构 |
| 默认模式容器与 Redis | 镜像构建、UID 10001、生产图合成模型、实际缓存/Lua/TTL、HTTP 限流、Redis 停机/恢复、API 重启持久化均通过 |
| 团队模式容器 | 单独镜像构建与 UID 10001 启动通过；真实 HTTP 验证匿名拒绝、跨空间拒绝、只读可读/不可写、成员撤销、独立文献库、退出会话撤销 |
| 团队容器重启 | 登录 Cookie 继续有效；原答案与 2 条索引数据保持；成员撤销状态保持 |
| 浏览器 | 实际登录、空间/账号管理页面、真实 PDF 原页加载及带空间前缀的下载链接、只读交互、切换空间并创建独立会话、退出返回登录页均观测通过 |

## 本地证据

- `output/container-redis-e2e-20261018-08/report.json`：默认模式容器与真实 Redis 的完整报告、各步骤事实和编号命令日志；包含启用真实 Redis 的完整后端测试日志。项目为 `deepsearch-e2e-6800aca91a79`。
- `output/team-container-e2e-20261018-01/report.json`：团队模式真实容器报告、配置、镜像身份与编号日志；项目为 `deepsearch-e2e-0b154e06564d`，镜像 ID 为 `sha256:169e15890392e56ef67166a03a7bbbaebb10d1bea23bf85991c0657428875319`。
- `output/team-access-ui-20261018-01/report.json`：人工编排浏览器操作的观测记录，非独立自动化浏览器测试运行器。截图位于同目录 `login.png`、`pdf-preview.png`、`workspace-management.png`、`readonly-workspace.png`。
- `tests/test_team_access.py`：可重复权限测试。外部 Embedding 被替换为确定向量；PDF 子进程、文件、图片渲染、附件、Wiki、会话、SQLite 与应用重启路径实际执行。

浏览器观察到原页图片完成加载（naturalWidth 893），下载链接含 `/w/{id}/api/documents/.../file`。只读成员可打开已有会话，创建会话与发送按钮禁用。切到 Beta 空间创建“Beta 独立会话”，返回 Alpha 后该会话不可见，Alpha 原会话仍可见。浏览器没有操作密码修改或授予权限；这些行为由后端测试在合成账号上验证。

两次容器运行均创建新项目、新命名卷与临时回环端口；完成后删除自身容器、1 个应用卷与专属应用镜像，保留报告和日志。没有挂载或清理用户现有资料目录，也没有修改现有部署账号。

## 实施中发现并修复

1. 新工作空间层级使 Windows 附件路径超过传统长度限制，原先保存返回 500；工作空间根转换为 Windows 扩展长度路径后，真实附件保存、读取和重启回归通过。原有证据 ID、数据表与索引格式不变。
2. 重置密码与登录签发会话存在并发窗口；现在签发事务中重新验证密码摘要和停用状态。重置后的旧摘要无法签发新会话，恢复停用账号也不会恢复旧 Token。
3. Pydantic 默认错误可能回显提交的密码；团队身份接口现在返回不含输入值的统一参数错误。
4. 浏览器多个标签页共用“当前空间”会使写入目标含糊；使用显式 URL 空间前缀和完整切换导航，并对 JSON、SSE、附件图像、原文预览和下载统一处理。

## 可重复命令

```powershell
.venv/Scripts/python.exe -m pytest tests/test_team_access.py tests/test_architecture.py tests/test_infrastructure_e2e.py -q
.venv/Scripts/python.exe -m deepsearch_experiments.architecture
.venv/Scripts/python.exe -m compileall -q src
git diff --check

.venv/Scripts/python.exe -m deepsearch_experiments.infrastructure_e2e `
  --docker-host 'npipe:////./pipe/dockerDesktopLinuxEngine' `
  --docker-config tmp/docker-config `
  --build-proxy 'http://host.docker.internal:7897' `
  --output output/container-redis-e2e-next

cd frontend
npm test
npm run build
```

`--output` 必须是新目录。团队容器验证使用本地诊断驱动 `tmp/run_team_container_acceptance.py`，复用同一独立容器验收器并显式覆盖 `ACCESS_MODE=team`、`ACCESS_COOKIE_SECURE=false`；合成管理员只在新容器卷中创建。浏览器测试服务同样只绑定回环端口，使用独立合成根目录。没有把浏览器快照或合成答案计为真实模型质量。

本机 npm 包装器的全局路径不可用，实际通过 `C:/Program Files/nodejs/node.exe` 调用同目录 `node_modules/npm/bin/npm-cli.js` 完成测试与构建，未更改用户全局设置。

## 未包含的验收

本轮没有对公网 HTTPS/网关部署、真实模型正确率、MCP 团队授权、SSO/MFA、多进程/多副本、任务队列、用户费用配额、自动备份与灾难恢复作通过声明。团队模式当前明确拒绝 MCP，默认仍为单用户模式；管理员初始化与实际启用步骤见 [团队操作说明](TEAM_ACCESS.md)。
