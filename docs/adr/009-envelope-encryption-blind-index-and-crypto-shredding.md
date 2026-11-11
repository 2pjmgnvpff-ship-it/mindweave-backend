# ADR-009：每用户字段级信封加密、盲索引与密码学销毁（Crypto-Shredding）

- **日期**：2026-11-12
- **状态**：`已采纳（Accepted）`
- **关联文档**：`PIA v0.2` 护盾 1/2、`PRD_v6.md` §7.3–7.4、`src/mindweave/core/crypto.py`、`src/mindweave/modules/data_rights/`
- **守护铁律**：I8（完整导出与删除）、I10（日志审计零正文）、I12（正文字段信封加密与密钥销毁）

---

## 1. 背景与问题

心络承载高度私密的人际关系备忘、语音转写与冲突树洞。仅依赖云硬盘静态加密无法防御以下风险：
1. 数据库快照、慢查询转储或越权只读账号直接窥探用户明文手帐；
2. 密文跨行/跨列剪切替换攻击（Ciphertext Transplantation）；
3. 用户依据《个人信息保护法》第 47 条行使注销删除权时，底层云数据库自动备份（PITR）窗口内的历史密文难以立即从物理磁道擦除；
4. 加密后的人物姓名与昵称如何在不暴露明文的前提下完成 `O(1)` 确定性实体消歧匹配。

---

## 2. 决策内容

1. **每用户独立 DEK 的信封加密（Envelope Encryption）**：
   - 每位用户创建账号时生成独立的 256-bit 数据加密密钥（DEK），由主密钥提供器（`KekProvider`：现阶段 `EnvKekProvider`，M6 切换阿里云 KMS `AliyunKmsProvider`）使用 AES-256-GCM 包裹并绑定 AAD `user_keys:wrapped_dek:{user_id}:{kek_id}` 后存入 `user_keys.wrapped_dek`；
   - 进程内 `EnvelopeCipher` 以有界 LRU + 5 分钟 TTL 缓存解包后的 `bytearray` DEK，驱逐或过期时逐字节覆写归零（`zeroize`）。

2. **全量正文字段与媒体对象强绑定上下文 AAD**：
   - 所有敏感文本列（`*_enc`：`display_name_enc`、`body_enc`、`content_enc`、`raw_text_enc`、`transcript_enc`、`whisper_enc` 等）采用统一二进制信封：`0x01 (version) || 12B nonce || ciphertext || 16B GCM tag`；
   - 字段级 AAD 严格绑定为 `f"{user_id}:{table}:{column}:{row_id}"`，对象存储媒体与导出包 AAD 绑定为 `f"{user_id}:blob:{object_key}"`。任何跨用户、跨表、跨列或跨行搬移密文的行为均在 GCM 认证阶段直接失败抛出 `CryptoError`。

3. **HKDF + HMAC-SHA256 租户隔离盲索引（Blind Index）**：
   - 针对 `person_aliases.alias_hmac`，通过 `HKDF-SHA256(ikm=DEK, salt=user_id, info="mindweave-blind-index-v1")` 派生租户专属盲索引子密钥，对规范化别名计算 HMAC-SHA256；
   - 既支持同租户内人物别名的精确等值查找，又保证不同用户记录相同名字时哈希值完全不同，彻底阻断跨账号撞库与社交图谱串联（落实 PIA 护盾 1）。

4. **数据主权闭环：GB 45438-2025 可携带导出与即刻密码学销毁（Crypto-Shredding）**：
   - **导出（`POST /v1/me/exports`）**：强制要求二次验证（`X-Reauth-Token`）与 `Idempotency-Key`；解密 `DataRightsRegistry` 注册的全部可导出表生成 `data.json`（内嵌符合 **GB 45438-2025** 的 `_aigcMetadata` 隐式溯源标识及内容 SHA-256 摘要）、按箭矢分层的可读 `journal.md`（附显式 AIGC 声明页脚）与 `media/*` 原始媒体；ZIP 包经用户 DEK 加密落盘，仅保存一次性下载令牌的 SHA-256 哈希，24 小时过期、单次下载即作废；
   - **注销（`DELETE /v1/me`）**：二次验证通过后，第一步在用户会话内将 `user_keys.wrapped_dek` 置空并写入 `destroyed_at`、同步清零内存 DEK 缓存（使一切残留或备份密文在密码学上永久不可逆）；第二步清理用户前缀下全部加密媒体对象；第三步取消排队后台任务并级联物理删除 `users` 及全部 21 张关联用户表记录，仅在 `audit_log` 留存 HMAC 匿名合规墓碑。

---

## 3. 后果与权衡

- 数据库无法对 `*_enc` 字段执行 `LIKE` 全文模糊检索，但这与 ADR-012（单人物全量解密组装上下文）天然契合，无需在数据库侧暴露明文索引。
- 新增任何用户业务表时必须同步注册至 `data_rights_registry` 并纳入导出/级联删除链路，否则架构门禁测试直接阻断合入。
