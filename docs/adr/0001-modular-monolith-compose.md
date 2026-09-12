# ADR-0001：采用模块化单体与 Docker Compose

## 状态

Superseded by [ADR-0006](0006-local-json-sqlite.md)（2026-09-12）。  
保留「模块化单体」边界；**未实现** Docker Compose / 多进程 Worker，当前为单进程 uvicorn + 线程池。

## 背景

项目由个人开发并在本机单用户运行，但需要 API、异步任务、数据库、向量索引和可恢复任务。微服务与 Kubernetes 会显著增加运维成本。

## 决策

使用模块化单体。API、Worker 和迁移服务共享后端代码与镜像，以 Docker Compose 编排独立进程。

## 后果

- 优点：边界清晰、部署简单、可独立运行长任务、容易本地调试。
- 缺点：模块不能独立发布；扩大为多用户云服务时可能需要拆分。
- 替代方案：纯单进程不利于长任务恢复；微服务和 Kubernetes 对当前规模过度设计。
