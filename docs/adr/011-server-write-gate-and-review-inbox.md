# ADR-011：服务端确定性写入门禁（`GatePolicy`）与待确认收件箱（`inbox_items`）

- **日期**：2026-11-12
- **状态**：`已采纳（Accepted）`
- **关联文档**：`ADR-004`、`ADR-005`（默认先行 1 & 2）、`BACKEND_MASTER_PLAN.md` §3.1–3.2
- **守护铁律**：I2（绝不丢失用户输入）、I3（入库门禁由服务端确定性策略决定）

---

## 1. 背景与问题

随口记与伴聊自动回写面临两类核心张力：
1. **零摩擦秒存 vs 防错防污染**：若每次录入都弹窗要求用户逐项审核，会摧毁“按住说一句就关屏”的低摩擦心流；但若不加门禁静默入库，重名消歧错误、微信截图左右气泡主客体颠倒、新人物静默建档或情侣吵架时的反讽气话将直接污染长期关系档案（“回旋镖陷阱” `PRT-01`）；
2. **大模型不确定性**：不能依赖提示词让大模型自己拍板是否入库，必须将“语义信号识别”与“入库状态裁决”解耦。

---

## 2. 决策内容

1. **原始输入先加密落库，AI 异常兜底为 `note`（铁律 I2）**：
   - `POST /v1/captures` 受理瞬间先将音频/文本密文持久化至 `captures(status="received")`，随后异步处理；
   - 若 ASR、多模态解析或结构化抽取超时/Schema 修复仍失败，自动将原始输入保存为 `Record(kind="note", review_state="note_only")` 与 `Fact(type="note", status="pending", pending_reason="extraction_failed")`，确保用户输入 0% 丢失。

2. **纯函数门禁策略 `GatePolicy.decide()`（铁律 I3）**：
   LLM 仅输出结构化信号（候选人物、置信度、事实类型、极性变化、说话人方向、气话/反讽标记），服务端纯函数按以下 8 条确定性规则判定：满足任一条即进入 `FactStatus.PENDING` 并生成 `inbox_items`，全部未命中才直接进入 `FactStatus.CONFIRMED`（附 5 秒撤销窗口 `undo_deadline`）：
   1. 抽取或 Schema 校验失败（`extraction_failed`）；
   2. 目标人物处于 `provisional` 待确认新建状态（`new_person`）；
   3. 实体解析候选人物数 `!= 1`（`ambiguous_person`）；
   4. 单句涉及多人且未被 `route_mode="current"` 锁定（`multi_person`）；
   5. 事实类型为 `date` 或 `promise`，需用户确认具体提醒时间（`date_or_promise`）；
   6. 识别出气话/反讽（`venting=True`）且对象为 `gold` 记爱人物（`venting`）；
   7. 截图或对话中 `source_speaker == "unknown"`（如群聊无法判定发言方，`speaker_unknown`）；
   8. 置信度低于阈值（默认 `0.80`）或发生既有偏好极性反转（`low_confidence` / `polarity_flip`）。

3. **新人物折中方案与气话 72 小时自动降级**：
   - 当全局随口记提及未知人物时，自动创建 `state="provisional"`、`tier="crystal"` 的临时人物并派发 `new_person_confirm` 收件箱项，其名下事实一律锁定在 `pending`，直至用户确认建档或合并至已有人物；
   - `venting_hold`（气话暂存）收件箱项支持一键 `[确认入库]`、`[仅存普通日记]` 或 `[丢弃]`；超过 72 小时未处理时，后台周期任务 `memory.expire_venting_inbox` 自动将其关联记录降级为 `note_only` 并将待定事实标为 `rejected`，避免陈旧气话混入伴侣正式画像。

---

## 3. 后果

- 伴聊上下文构建器严格硬过滤 `WHERE user_id = :uid AND person_id = :pid AND status = 'confirmed' AND invalid_at IS NULL`，所有未确认、已撤销或仅存日记的内容从数据结构层面被排除在 AI 长期记忆之外。
