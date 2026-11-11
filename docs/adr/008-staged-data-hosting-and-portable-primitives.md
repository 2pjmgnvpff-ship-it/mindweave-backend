# ADR-008：分阶段数据托管（Supabase → 境内 RDS/OSS）与零厂商锁定基础设施原语

- **日期**：2026-11-12
- **状态**：`已采纳（Accepted）`
- **关联文档**：`ADR-002`、`BACKEND_MASTER_PLAN.md` §2 & §6、`src/mindweave/core/db.py`、`migrations/versions/0001_baseline_baseline_schema_and_rls_policies.py`
- **守护铁律**：I1（PostgreSQL RLS 第二道防线）、I8（数据可携带与删除）、I12（对象存储服务端加密）

---

## 1. 背景与问题

M0–M5 阶段选用托管 PostgreSQL（如 Supabase）可加速迭代，但 M6 面向境内用户正式上线前，数据库、对象存储与密钥管理必须完整迁回境内云基础设施（阿里云 RDS PostgreSQL 16 + OSS + KMS）。

若早期依赖 Supabase 专有生态（如 `auth.users` 外键、`auth.uid()` RLS 函数、Supabase Storage 私有鉴权策略或 Supabase Realtime），M6 迁移时将遭遇密码哈希/会话不可迁移、RLS 策略全面重写以及客户端直传鉴权断裂。

---

## 2. 决策内容

1. **数据库层仅使用标准 PostgreSQL 16 原生能力**：
   - 使用 Alembic 管理全部 DDL 与迁移脚本，不使用任何云厂商私有 SQL 扩展；
   - **双连接池最小权限隔离（`DatabaseManager`）**：
     - `app_engine`（`mindweave_app` 角色，强制 `rolsuper = false` 且 `rolbypassrls = false`）：承载全部常规用户请求；
     - `system_engine`（`mindweave_system` 角色，`BYPASSRLS`）：严格限制于认证引导、后台队列认领、定时提醒扫描与合规脱敏审计写入。
   - **全部 19 张用户域表同时开启 `ENABLE ROW LEVEL SECURITY` 与 `FORCE ROW LEVEL SECURITY`**：
     - 每个用户事务开头执行 `SELECT set_config('app.user_id', :uid, true)`（`SET LOCAL` 事务级绑定，连接归还连接池时自动失效，杜绝跨请求串租户）；
     - 统一策略 `tenant_isolation` 使用标准 SQL：`USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)` 与相同 `WITH CHECK`，未设置会话变量时查询返回空集且禁止写入。

2. **自建轻量身份认证模块（不依赖托管 BaaS Auth）**：
   - 自托管 `users`、`auth_identities`（Apple JWKS + 邮箱 6 位 OTP，M6 扩展境内短信 OTP）与 `refresh_tokens`（Token Family 轮换与重放检测，数据库仅存 SHA-256 哈希）；
   - Access Token（15 分钟 TTL）与高敏二次验证 Step-Up Token（`X-Reauth-Token`，5 分钟 TTL）统一由后端 `TokenService` 签发与验签，跨云迁移时用户账号与登录态零损平移。

3. **对象存储统一走服务端加密流转（禁用客户端预签名明文直传）**：
   - 通过 `ObjectStoragePort` 抽象底层存储（本地加密文件存储 `LocalEncryptedBlobStore` / S3 兼容协议适配 Supabase Storage 与阿里云 OSS）；
   - 所有音频、保留截图、自定义壁纸与导出压缩包均在 API/Worker 内存中使用用户 DEK 完成 AES-256-GCM 加密后再落盘/上传，确保底层存储桶永不接触明文。

---

## 3. 后果与验证守护

- 架构不变量测试（`tests/architecture/test_architecture_invariants.py`）直接查询 PostgreSQL 系统目录 `pg_class`、`pg_policies` 与 `pg_roles`，在每次 CI 中核验全部 19 张表的 `relrowsecurity`、`relforcerowsecurity`、`tenant_isolation` 策略以及 `mindweave_app` 的 `NOBYPASSRLS` 属性。
- M6 从海外测试库切换至境内阿里云 RDS + OSS 仅需执行 `pg_dump/pg_restore`（或逻辑复制）与对象桶同步，应用层业务代码零修改。
