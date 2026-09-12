# DeepSearch 前端

React 19 + TypeScript + Vite 7。本地连接项目现有 FastAPI（默认 `127.0.0.1:8000`）。

当前环境为 Node.js 22.18+ 或 24+（脚本使用 `--configLoader native` 直接加载 TypeScript 配置，避免 Windows 沙箱下 esbuild 扫描父目录失败）。

## 启动

先在项目根目录的 Python 虚拟环境中运行后端：

```powershell
python -m uvicorn deepsearch_agent.api:app --reload --host 127.0.0.1 --port 8000
```

在另一个终端进入本目录：

```powershell
npm ci
npm run dev
```

本机 npm 全局启动脚本异常时，可使用 `start.ps1`，或：

```powershell
$nodeExecutable = (Get-Command node).Source
$npmEntry = Join-Path (Split-Path $nodeExecutable) 'node_modules\npm\bin\npm-cli.js'
& $nodeExecutable $npmEntry run dev
```

打开 http://127.0.0.1:5173 。Vite 已将 `/api` 代理到 8000。

## 验证

```powershell
npm test
npm run build
```

## 页面功能（与当前实现一致）

- 文献列表、名称筛选、PDF 上传（后台解析轮询）、导入演示文献、刷新状态
- 本地知识库快照：保存 / 重新加载、脏标记与写入保护提示
- 多会话：创建、切换、删除；会话内提问与 SSE 流式阶段/回答
- 结构化记忆：研究目标、语言、回答风格、文献范围（与左侧勾选同步）
- 证据卡：页码、原文片段、复制回答时附带证据；点击打开 PDF 页预览
- 选定文献范围检索；空状态、无证据、断连与超时反馈
- 响应式布局、键盘焦点、中文标签、减少动态效果支持

演示模式仅支持后端内置固定问题；真实模式需配置 `.env` 与 `.env.embedding`。

## 保存与恢复

左侧「本地快照」显示后端返回的保存状态，并提供「保存知识库」和「重新加载」。导入后由后端自动保存，后端启动时自动恢复快照。

- `GET /api/knowledge-base/status`：快照是否存在、未保存状态、写入保护及错误
- `POST /api/knowledge-base/save` / `load`：保存 / 从磁盘快照替换内存知识库

写入受保护时禁用保存和导入，仍允许加载修复后的快照。状态接口返回 404 时请重启已更新的后端。前端不接收磁盘路径。

## 文件职责

- `src/api.ts`：HTTP、超时、错误处理、Zod 校验与类型
- `src/App.tsx`：布局、文献库、上传与快照
- `src/Conversations.tsx`：会话、流式问答、记忆与证据
- `src/PdfViewer.tsx` / `pdfSource.ts`：页预览
- `src/persistence.ts`：快照状态文案与按钮规则
- `vite.config.ts`：5173 端口与 `/api` → 8000 开发代理

API Key 不应写进前端。页面未使用外部字体/图片/CDN。Vite 开发代理不等于生产部署；生产需单独配置 `/api` 转发。
