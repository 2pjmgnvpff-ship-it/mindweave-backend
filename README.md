# MindWeave (心络) Backend

Privacy-first relationship memory, multimodal capture, and Cupid companion backend built on **Python 3.12 + FastAPI + PostgreSQL (`FORCE RLS` + Per-User AES-256-GCM Envelope Encryption)**.

[![Run on Google Cloud](https://deploy.cloud.run/button.svg)](https://deploy.cloud.run/?git_repo=https://github.com/2pjmgnvpff-ship-it/mindweave-backend)

---

## 1. 一键获取 Google Cloud Run 公共链接 (`https://*.run.app`)

点击上方 **Run on Google Cloud** 按钮（或在 [Google Cloud Run Console](https://console.cloud.google.com/run) 选择 **Deploy Container -> Continuously deploy from a repository** 导入本仓库）：

- **零配置即开即测**：如果不配置外部数据库环境变量，容器启动时会自动拉起内嵌 PostgreSQL 15（完整启用 19 张表 `FORCE RLS` 行级隔离 + Alembic 迁移 + 内嵌异步 Worker），自动生成公网链接 `https://mindweave-backend-xxxx.run.app`（访问根路径 `/` 即进入全链路实测工作台，访问 `/docs` 进入 Swagger UI）。
- **接入 Google AI Studio 真实 Gemini 模型（可选）**：在环境变量中填入你在 Google AI Studio 获取的 `MINDWEAVE_GEMINI_API_KEY`，服务会自动切换至真实 `gemini-2.5-flash` 模型。
- **挂载持久化 Supabase PostgreSQL（可选）**：在 Supabase SQL Editor 执行一次 [`scripts/init_supabase_roles.sql`](./scripts/init_supabase_roles.sql)，随后在 Cloud Run 配置 `MINDWEAVE_APP_DATABASE_URL`、`MINDWEAVE_SYSTEM_DATABASE_URL`、`MINDWEAVE_MIGRATION_DATABASE_URL` 及 `MINDWEAVE_MIGRATE_ON_BOOT=true`。

---

## 2. 本地 Docker + Cloudflare 公网穿透一键启动

```bash
docker compose --profile tunnel up -d --build
docker compose logs -f tunnel
```

---

## 3. 架构总纲、AI 规则规范与门禁命令

详见 [`docs/BACKEND_MASTER_PLAN.md`](./docs/BACKEND_MASTER_PLAN.md)、[`docs/AI_INTEGRATION_AND_RULES_SPEC.md`](./docs/AI_INTEGRATION_AND_RULES_SPEC.md)、[`AGENTS.md`](./AGENTS.md) 与 [`docs/adr/`](./docs/adr/)。

```bash
make lint && make typecheck && make arch
make test   # 含单元、架构、Postgres RLS 集成与红队对抗测试
make eval   # 离线评测集
```

### 一键初始化演示工作区（Demo Day Showcase）

```bash
uv run python scripts/seed_demo.py --email demo@mindweave.app --reset
```

该脚本会在真实 PostgreSQL（`FORCE RLS` + 按用户 AES-256-GCM 信封加密）中预置 `妈妈`（金箭 · 18 条跨年手帐与真实可播放 WAV 语音）、`阿远`（金箭 · 6 条手帐）、`陈经理`（银箭 · 零照片事实边界记录）及 4 位 `记繁琐` 联系人。开发环境启动后端后，在首页输入 `demo@mindweave.app` 发送验证码即可自动回填一键登录。

---

## 4. 切换 AI 提供方与语音识别通道

通过环境变量 `MINDWEAVE_AI_PRIMARY_PROVIDER`（可选降级通道 `MINDWEAVE_AI_FALLBACK_PROVIDER`，以及可选独立语音通道 `MINDWEAVE_AI_ASR_PROVIDER`）即可在五种提供方之间切换（任务路由见 [`src/mindweave/ai/routing.yaml`](./src/mindweave/ai/routing.yaml) 与 [`docs/AI_INTEGRATION_AND_RULES_SPEC.md`](./docs/AI_INTEGRATION_AND_RULES_SPEC.md)）：

| 提供方 | `MINDWEAVE_AI_PRIMARY_PROVIDER` | 必填密钥 / 模型环境变量 | 说明 |
|---|---|---|---|
| 离线确定性桩 | `fake` | 无需密钥 | 本地开发与离线 E2E 默认项；`prod_cn` 环境禁止启用 |
| Google Gemini | `gemini` | `MINDWEAVE_GEMINI_API_KEY`（可选 `MINDWEAVE_GEMINI_MODEL_FAST` / `MINDWEAVE_GEMINI_MODEL_CHAT`，默认 `gemini-2.5-flash`；`MINDWEAVE_GEMINI_PAID_TIER_ACKNOWLEDGED`） | 原生支持音频转写与多模态；仅用于内部自测与非国内环境，`prod_cn` 强制拦截 |
| 阿里百炼（通义千问） | `dashscope` | `MINDWEAVE_DASHSCOPE_API_KEY`（默认 `qwen-plus` / 视觉 `qwen-vl-max` / 语音 `MINDWEAVE_DASHSCOPE_ASR_MODEL=qwen-audio-turbo`，可覆盖） | 中国大陆合规备案模型之一，支持多模态音频 `input_audio` |
| 火山方舟（豆包） | `ark` | `MINDWEAVE_ARK_API_KEY`（可选 `MINDWEAVE_ARK_CHAT_MODEL` 默认 `doubao-1-5-pro-32k`、`MINDWEAVE_ARK_BASE_URL`） | 中国大陆合规备案模型之一 |
| DeepSeek | `deepseek` | `MINDWEAVE_DEEPSEEK_API_KEY`（可选 `MINDWEAVE_DEEPSEEK_CHAT_MODEL` 默认 `deepseek-chat`、`MINDWEAVE_DEEPSEEK_BASE_URL`） | 严禁配置 `deepseek-reasoner` / `*-r1` 推理模型（遵循 ADR-001 首字延迟上限）；可配合 `MINDWEAVE_AI_ASR_PROVIDER=dashscope` 处理语音 |


