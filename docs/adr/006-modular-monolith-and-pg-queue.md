# ADR-006：Python 3.12 + FastAPI 模块化单体、六边形分层与 PostgreSQL 原生异步队列

- **日期**：2026-11-12
- **状态**：`已采纳（Accepted）`
- **关联文档**：`BACKEND_MASTER_PLAN.md` §1.1–1.2、V10 规范 §四（落定路线 A）
- **守护铁律**：I1（租户与人物硬隔离）、I8（`DataRightsRegistry` 完整性）、I9（厂商中立）、I11（UUIDv7 写接口幂等）

---

## 1. 背景与问题

心络（MindWeave）需要一套同时服务 Web V10（`gleb-app.mjs`）与 iOS SwiftUI（`MindWeaveStore.swift`）的生产级后端，承载多模态随口记（语音/文字/截图）、人物档案与三态箭矢排序、丘比特 SSE 流式伴聊、农历/公历到点提醒以及 PIPL 数据导出与注销。

在架构形态上存在三组核心权衡：
1. **微服务 vs 模块化单体**：单用户关系规模收敛（通常 3–15 位核心人物、单人物有效事实 ≤ 100 条），研发团队精简，早期拆分微服务会引入分布式事务与跨服务鉴权开销；
2. **语言与运行时（V10 路线 A Python FastAPI vs 路线 B TypeScript Hono）**：AI 抽取校验、流式引用剥离、农历换算（`lunar-python`）、信封加解密（`cryptography`）及离线/在线评测管线均高度集中在 Python 生态；
3. **异步任务基础设施（独立 Redis/Celery vs PostgreSQL 原生队列）**：引入额外缓存/消息中间件会增加 M0–M5（Supabase）向 M6（阿里云 RDS）迁移时的运维面与一致性风险。

---

## 2. 决策内容

1. **采用 Python 3.12 + FastAPI 模块化单体（Modular Monolith）**：
   - 单一代码库按业务限界上下文划分为 `identity`、`preferences`、`people`、`bootstrap`、`capture`、`memory`、`companion`、`reminders`、`safety`、`data_rights`、`telemetry`、`audit` 模块，以及横切层 `core`、`shared`、`ai`、`workers`；
   - API 与异步 Worker 共享同一套领域模型与配置，以不同进程入口运行（`uvicorn` API 进程 + `WorkerRunner` 队列消费进程）。

2. **模块内部强制执行六边形分层（依赖单向向内）**：
   - `api/`：FastAPI `APIRouter` 与 `CamelModel` 请求/响应 DTO（对外统一 camelCase + RFC 7807 `application/problem+json`）；
   - `application/`：用例编排、幂等键与事务边界、跨模块公共服务调用；
   - `domain/`：100% 纯 Python 领域实体、值对象与确定性策略函数（严禁导入 `fastapi`、`sqlalchemy`、`asyncpg`、`httpx`、任何厂商 SDK 或 `mindweave.core.{orm,db,dependencies}`）；
   - `infra/`：SQLAlchemy 2.0 异步仓储实现与外部基础设施适配器。
   - 依赖边界由根目录 `.importlinter` 契约定义，并在 CI 与架构测试（`lint-imports`）中强制阻断违规提交。

3. **采用 PostgreSQL `FOR UPDATE SKIP LOCKED` 原生任务表替代独立消息队列**：
   - 通过 `background_jobs` 表承载异步任务（`capture.process`、`companion.writeback_facts`、`companion.summarize_thread`、`data_rights.build_export` 等）与周期调度任务；
   - `WorkerRunner` 基于租约（`locked_until`）、指数退避重试（`min(3600, 2 ** attempts * 5)` 秒）与死信状态（`dead`）保证至少一次投递，且 `last_error` 强制经过零 PII 脱敏截断。

---

## 3. 后果与演进路径

- **正面收益**：零 Redis 依赖，事务内入队天然具备原子性；Supabase PostgreSQL 16 与境内阿里云 RDS PostgreSQL 16 100% 兼容；纯领域层可在无数据库/网络环境下毫秒级完成全矩阵单测。
- **约束与演进触发条件**：当单库后台任务吞吐持续超过 `500 jobs/s` 或锁竞争导致轮询延迟 P95 > 500ms 时，仅需实现新的 `JobQueuePort` 适配器并将 `workers` 独立扩容部署，业务模块无需改动。
