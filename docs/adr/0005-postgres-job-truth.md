# ADR-0005：PostgreSQL保存任务真相，Redis只做投递

## 状态

Accepted

## 背景

PDF 导入、Embedding 和 DeepSearch 都是长任务。Redis 或 Worker 重启时，任务进度不能永久丢失。

## 决策

任务阶段、进度、检查点、心跳和错误保存在 PostgreSQL。Redis/Celery 消息只包含任务 ID。Worker 通过幂等操作和数据库检查点恢复任务。

## 后果

- 优点：Redis 丢失后可重建任务队列；任务历史可审计；前端不依赖 Celery result backend。
- 缺点：业务代码需要显式维护任务状态和恢复逻辑。
- 替代方案：只使用 Celery 状态更简单，但不能满足可恢复性和长期审计要求。
