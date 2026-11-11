# ADR-007：AI Model Gateway 与分阶段模型供应商策略（Gemini → 境内备案模型）

- **日期**：2026-11-12
- **状态**：`已采纳（Accepted，M0–M5 内部自测豁免，M6 上线闸门强制切换）`
- **关联文档**：`ADR-001`、`BACKEND_MASTER_PLAN.md` §4 & §8、`src/mindweave/core/config.py`
- **守护铁律**：I5（安全独立通道）、I9（厂商中立与评测门禁）、I10（计量零正文）

---

## 1. 背景与问题

研发期（M0–M5）需要利用多模态大模型快速打通音频直入转写纠错、微信截图左右气泡识别、结构化抽取与流式伴聊闭环；而面向中国境内公众正式提供服务前（M6），依据《个人信息保护法》第 38–40 条与《生成式人工智能服务管理暂行办法》，真实用户数据严禁违规出境，且必须切换至已完成网信办生成式 AI 服务备案的境内模型。

若在业务代码中直接耦合 `google-genai` SDK 或特定模型输出习惯，M6 切换至通义千问（DashScope）、豆包（火山方舟 Ark）或 DeepSeek-V3 时将面临大规模重写与回归失控。

---

## 2. 决策内容

1. **统一收口至 `ai/` 六边形网关（`ModelGateway` + Ports）**：
   - 业务模块仅依赖抽象端口（`LLMPort` / `ASRPort`），按任务语义路由：`extract`、`chat`、`safety_classify`、`transcribe`、`screenshot`、`summarize`；
   - `.importlinter` 强制校验：除 `mindweave.ai.adapters` 外，任何模块导入 `google` SDK 直接导致 CI 失败；
   - 所有 Prompt 以带 YAML front-matter（`id`、`version`）的独立文件管理，计算内容 SHA-256 哈希，并将 `prompt_version` 写入 `captures`、`facts`、`chat_messages` 与 `ai_calls`。

2. **分阶段供应商策略与代码级合规硬闸门**：
   - **研发自测阶段（`environment ∈ {dev, test, staging}`，M0–M5）**：允许使用 `fake`（确定性离线测试适配器）或付费层 `gemini`（强制启用企业级不留存/不用于训练配置；严禁向公众开放注册，仅限开发者自用或合成测试数据）；
   - **境内生产阶段（`environment = "prod_cn"`，M6 准出）**：`Settings._validate_production_guardrails` 在进程启动期强制拦截——一旦检测到 `ai_primary_provider == "gemini"` 或 `ai_fallback_provider == "gemini"`，立即抛出 `ValueError` 拒绝启动；
   - **推理模型禁令**：配置校验器强制拦截 `deepseek-reasoner` / `-r1` 型号，避免长思维链拖慢伴聊首字响应延迟（TTFT > 1.5s）。

3. **可靠性、前置硬 PII 掩码与零正文计量**：
   - 调用外部模型前，统一经 `mindweave.shared.pii.mask_hard_pii` 确定性掩码 18 位身份证号（含 ISO 7064 校验位）、Luhn 银行卡号与明文密码口令，同时保留人物昵称与城市日期以保障指代消解准确率；
   - 结构化输出校验失败时自动修复重试 1 次，仍失败则交由上层按铁律 I2 降级保存为 `note`；
   - **严禁启用跨轮或跨用户语义缓存（Semantic Cache）**，仅允许云厂商侧无状态系统提示词前缀缓存；
   - `ai_calls` 表仅记录 `task`、`provider`、`model`、`prompt_version`、进出 Token 数、延迟与微元成本，**零提示词与零回复正文**。

---

## 3. 后果与 M6 切换清单

- M6 切换仅需在 `evals/` 评测集（≥150 条含红队与三支柱样本）上完成 `dashscope`（Qwen-Plus + Paraformer）、`ark`（Doubao-pro）与 `deepseek`（DeepSeek-V3）的实测对比，确保**跨人串扰率 = 0、虚构引用率 = 0、危机召回率 = 100%** 且归档准确率不低于基线后，通过环境变量切换路由即可上线。
