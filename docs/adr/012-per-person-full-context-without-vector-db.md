# ADR-012：不引入向量数据库——按 `PersonScope` 全量拉取记忆与可核验引用流式校验

- **日期**：2026-11-12
- **状态**：`已采纳（Accepted）`
- **关联文档**：`PRD_v6.md` §6 契约 2、`BACKEND_MASTER_PLAN.md` §3.3、Lane 2 架构减法（删-01/02）
- **守护铁律**：I1（`PersonScope` 硬隔离）、I4（引用必须可验证 `cited ⊆ ctx`）、I12（密文存储）

---

## 1. 背景与问题

在“问问这段关系”（丘比特伴聊）场景中，常见做法是引入独立向量数据库（Milvus/Pinecone/Qdrant）或端侧向量索引做 Top-K 语义检索（RAG）。然而在本项目的业务形态与隐私约束下，过早引入向量检索存在三大致命缺陷：
1. **Top-K 截断导致关键忌口/雷区漏检**：用户问“周末请清秋吃饭订哪家”，语义向量可能召回“喜欢日料”，却漏掉早期记录的“对芒果与花生严重过敏”（`taboo`）；
2. **跨人记忆串扰风险**：多人物共享向量索引一旦过滤条件疏漏，极易将前任或同事的记忆混入当前伴侣对话（违反 P0 零串扰红线）；
3. **明文向量泄露风险与加密冲突**：正文字段已采用每用户 DEK 信封加密（ADR-009），若持久化明文 Embedding 向量，既增加向量反演攻击面，又破坏纯粹的密文落库模型。

---

## 2. 决策内容

1. **MVP 至中期不引入任何向量数据库或图数据库**：
   - 依据真实关系规模测算，每位人物名下有效已确认事实通常不超过 100 条（折合上下文约 2k–4k tokens），现代长窗口大模型一次全量读取既更准又更快；
   - 仅借鉴双时序字段（`valid_at`、`invalid_at`、`superseded_by_id`）表达事实随时间的更替，不引入 Neo4j/Graphiti 图存储组件。

2. **唯一合法的上下文读取路径：`PersonScope(user_id, person_id)` 全量确定性组装**：
   - `ContextBuilder` 强制接收 `PersonScope` 值对象，仅查询该单个人物的：
     1. 人物基础档案与特征标签；
     2. 全部有效已确认事实（`status = 'confirmed' AND invalid_at IS NULL`）；
     3. 用户在右滑素材匣中显式勾选的 `referencedMaterialIds` 正文，以及事实关联的证据 `records` 摘要；
     4. 当前人物专属 `chat_thread` 的滚动摘要与最近 12 轮对话消息。
   - **确定性预算裁剪策略**：当超长人物档案逼近单轮上下文预算上限时，`taboo`（忌口/雷区）、`date`（重要日子）与 `promise`（待兑现承诺）100% 强制保留，其余类型按 `valid_at DESC` 截断补齐。

3. **流式可核验引用校验器（`StreamingCitationProcessor`，铁律 I4）**：
   - 注入模型的每条原始记录携带白名单标识 `[rec_<id>]`，模型输出经流式处理器实时解析；
   - 校验器核对 `cited_id ∈ allowed_context_record_ids`：合法引用转换为结构化 `citation{index, materialId, title, snippet}` SSE 事件下发前端渲染为精致角标；**凡不在本轮上下文白名单内的虚构引用一律当场剥离**，且原始 `[rec_...]` 字符串绝不直接暴露到前端展示文本中（铁律 I6）。

---

## 3. 重新评估向量检索的量化触发条件

仅当线上匿名统计满足以下任一指标时，才启动 `pgvector` 方案评估：
1. 单人物有效 `confirmed` 事实数 P95 > **300 条**，或关联 `records` 数 > **500 条**；
2. 伴聊单轮组装后的输入上下文 P95 > **8,000 tokens**，且首字延迟 P95 突破 **1.5s**。

届时即使引入 PostgreSQL 原生 `pgvector` 扩展，也**必须**遵循以下前提：`taboo / date / promise` 仍走结构化全量必选，向量检索仅用于长篇历史日记 `records` 的补充召回，且 SQL 查询与 RLS 强制前置绑定 `WHERE user_id = :uid AND person_id = :pid`。
