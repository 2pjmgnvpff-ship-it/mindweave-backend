# ADR-010：规范三态箭矢 `tier` 枚举、分数索引排序与 `Capture / Record / Fact` 领域模型统一

- **日期**：2026-11-12
- **状态**：`已采纳（Accepted）`
- **关联文档**：`PRD_v6.md` §4 & §6、V10 规范 §一–二、`src/mindweave/shared/enums.py`、`src/mindweave/shared/lexorank.py`
- **守护铁律**：I4（可核验引用单位）、I6（展示文案零开发术语）、I7（切换箭矢零删历史）

---

## 1. 背景与问题

在前期多轮设计与原型演进中，仓库内形成了三套存在命名冲突的模型体系：
1. **关系层级枚举不一致**：PRD v6 与 iOS 原型使用 `love / matter / hate`，而定稿 Web V10（`gleb-app.mjs`）使用三态箭矢 `gold / silver / crystal` 并拆分为底部独立头像胶囊 `coreIds`（金箭 + 银箭）与名录抽屉 `casualIds`（水晶箭）；
2. **记录载体粒度断层**：V10 规范定义了 `scrapbook_materials`（拍立得、便签、原声签），但缺少结构化事实、双时序失效链与待确认门禁状态；PRD v6 定义了 `Record` 与 `Fact`，但未涵盖多人物长语音一次输入拆分为多张人物便签（`splits[]`）的摄入生命周期；
3. **整数排序锁竞争**：V10 初稿使用 `sort_order INT` 维护胶囊顺序，在高频拖拽与箭矢升降级（如金箭降为水晶箭插入 `casualIds` 首位）时需批量重写多行序号。

---

## 2. 决策内容

1. **数据库与 API 唯一规范箭矢枚举锁定为 `tier ∈ {gold, silver, crystal}`**：
   - `gold`（金箭 · 记爱）与 `silver`（银箭 · 记恨）映射至 `coreIds`；`crystal`（水晶箭 · 记繁琐）映射至 `casualIds`，且 `crystal` 默认允许 `has_photo = false`（前端使用单字印章 `monogram` 渲染）；
   - `Tier.from_any()` 在边界输入侧提供对历史旧值 `love → gold`、`hate → silver`、`matter/casual → crystal` 的向后兼容转换，但持久化列（带 `ck_persons_tier` 约束）与响应 JSON 统一只输出规范值。

2. **三层记忆实体统一：`captures` → `records`（对外 API 暴露为 `materials`）→ `facts`**：
   - **`captures`（一次原始摄入）**：对应一次全局/当前人物语音、文字、截图或伴聊回写任务，完整承载状态机（`received → transcribing → extracting → routed → completed | needs_review | saved_as_note | reverted`）与 5 秒撤销截止时间；
   - **`records`（手帐原始材料 = 唯一引用核验单位）**：一次全局输入可按人物拆分为多条 `records`（`kind ∈ {photo, note, voice}`）；伴聊内部的 `[rec_id]` 引用校验器严格核验 `record_id`，REST 接口以 `materials` 路径与字段形状对齐 V10 前端手帐抽屉；
   - **`facts`（双时序结构化事实）**：挂接于 `records` 之下，承载 `preference / taboo / date / wish / promise / event / note` 及 `pending / confirmed / rejected` 状态；当事实发生变更（如忌口或心愿更新）时，旧事实写入 `invalid_at` 与 `superseded_by_id`，严禁物理覆写历史记忆。

3. **采用 LexoRank 分数索引（`persons.rank`）与零损箭矢切换（铁律 I7）**：
   - 同分组内的顺序通过字典序字符串 `rank`（`rank_before` / `rank_after` / `rank_between`）单行 `O(1)` 更新；
   - 切换人物 `tier` 时仅原子更新 `persons.tier` 与 `persons.rank`，严禁软删或清理该人物名下的任何 `records`、`facts`、`reminders` 或 `chat_messages`。

---

## 3. 后果

- V10 前端无需改动现有 `coreIds`、`casualIds`、`PEOPLE_DB` 与 `materials` 渲染状态机即可无缝接入 `/v1/bootstrap` 与人物接口，同时后端完整具备结构化记忆门禁、到期提醒派生与精确证据溯源能力。
