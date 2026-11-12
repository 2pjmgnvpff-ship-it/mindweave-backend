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

## 3. 架构总纲与门禁命令

详见 [`docs/BACKEND_MASTER_PLAN.md`](./docs/BACKEND_MASTER_PLAN.md)、[`AGENTS.md`](./AGENTS.md) 与 [`docs/adr/`](./docs/adr/)。

```bash
make lint && make typecheck && make arch
make test   # 287 passed (含 43 项红队对抗回归测试)
make eval   # 65/65 离线评测集 100% 通过
```
