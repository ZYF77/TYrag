# 客户 JWT Token Claims 对接确认书

**文档状态**: 待客户确认
**版本**: 1.1
**日期**: 2026-08-05
**关联代码**: WP-01A (JWTValidator, UserPrincipal, require_user_principal, ext_user_map, GET /enterprise/api/v1/auth/me)

---

## 1. 目的

本文档供客户设备管理系统开发人员与 Enterprise RAG Gateway 团队之间确认 JWT Token 的身份协议。所有字段名、数据类型、签名方式均需客户侧确认后填入，Gateway 侧通过配置化 claim mapping (`JWT_CLAIM_MAP`) 适配，无需修改代码。

本文档**不包含任何真实客户信息**，所有示例值均为虚构测试数据。

## 2. 当前实现摘要

| 组件 | 文件 | 职责 |
|---|---|---|
| JWTValidator | `enterprise/gateway/auth/token_validator.py` | 签名验证、iss 校验、aud 校验（配置后）、exp 校验、JWKS 密钥获取 |
| JWTValidatorConfig | 同上 | 通过环境变量配置所有验证参数和 claim 映射 |
| UserPrincipal | `enterprise/gateway/auth/user_principal.py` | 从已验证 claims 构建用户身份主体 |
| require_user_principal | `enterprise/gateway/auth/middleware.py` | FastAPI 依赖注入，校验 Token 并检查 ext_user_map 状态 |
| ExtUserMapRepo | `enterprise/gateway/models/ext_user_map.py` | 用户映射持久化。当前实现为 SQLite（开发与当前验证）；生产目标为 PostgreSQL |
| GatewayConfig | `enterprise/gateway/config.py` | 集中环境变量配置 |

**关键设计原则**:
- Gateway 只通过 `JWT_CLAIM_MAP` 环境变量配置字段映射，不硬编码客户字段名
- 用户身份只来自已验证的 JWT claims，**绝不**来自请求正文
- Service Token（WP-02A）与 User JWT 严格分离，互相不可混用
- 当前用户映射持久化使用 SQLite；PostgreSQL Repository、迁移和连接池尚未实施，生产上线前必须完成相应迁移和并发验证

---

## 3. A. Token 类型确认

| 项目 | 当前测试默认值 | 客户确认值 | 必填 | 备注 |
|---|---|---|---|---|
| Token 格式 | 标准 JWT (RFC 7519) | **待确认** | 是 | 当前实现仅支持标准 JWT。若客户使用 opaque token，需额外开发 introspection endpoint 集成 |
| 是否存在 Refresh Token | 无 | **待确认** | 否 | Gateway 当前只处理 Access Token，不参与刷新流程 |
| Gateway 接收的 Token 类型 | Access Token only | **待确认** | 是 | 如果客户签发 ID Token 和 Access Token 分离，请注明 Gateway 应接收哪种 |
| Token 传递方式 | HTTP `Authorization: Bearer <token>` | **待确认** | 是 | 当前只支持 Bearer scheme |

---

## 4. B. 签名方式确认

| 项目 | 当前测试默认值 | 客户确认值 | 必填 | 备注 |
|---|---|---|---|---|
| 签名算法 | RS256, ES256 | **待确认** | 是 | 支持: RS256/384/512, ES256/384/512, PS256/384/512, EdDSA。HMAC (HS*) 默认禁用，需显式开启 `JWT_ENABLE_HS=true` |
| Issuer (`iss`) | `https://auth.example.com` | **待确认** | 是 | Gateway 通过 `JWT_ISSUER` 配置，与 Token 中 `iss` 严格比对 |
| Audience (`aud`) | `tyrag-gateway` | **待确认** | **是（生产）** | 生产环境必填并必须验证；仅本地开发或显式测试模式可关闭。`JWT_AUDIENCE` 为空时跳过验证属于当前实现能力，不是推荐生产配置；用于防止其他系统的 Token 被 Gateway 接受 |
| JWKS URL | 无 (测试用 HS256) | **待确认** | 是 | 生产必须配置 `JWT_JWKS_URL`。Gateway 通过 PyJWKClient 获取公钥 |
| 公钥交付方式 | 无 | **待确认** | 是 | 生产推荐 JWKS endpoint；静态公钥文件当前尚未实现（当前仅支持 JWKS URL 或 HMAC 共享密钥） |
| `kid` 和密钥轮换 | 无 | **待确认** | 是 | 当前代码可配置：PyJWKClient 支持 `kid` 匹配和缓存（默认 TTL 300s，可通过 `JWT_JWKS_CACHE_TTL` 配置）；轮换周期与通知方式由客户确认 |
| 是否强制 HTTPS for JWKS | 未强制 | **待确认** | 否 | 生产推荐使用 HTTPS；当前代码未强制 URL 协议 |
| Token 最大有效期 | 3600s (测试默认) | **待确认** | 是 | 当前代码每次请求校验 `exp`（过期即 401），但未强制最大有效期；≤ 1 小时为生产推荐，客户待确认 |
| Clock skew 容忍 | 默认 PyJWT 行为 (约 0s) | **待确认** | 否 | PyJWT 默认 leeway=0s；`JWTValidatorConfig` 未暴露 leeway 配置项（尚未实现），如需调整需代码变更 |

### 4.1 实现状态与推荐策略对照

| 项目 | 状态 | 说明 |
|---|---|---|
| 签名算法限制 | 当前代码可配置 | `JWT_ALLOWED_ALGS` 白名单；HMAC 默认禁用，需显式开启 |
| Issuer 校验 | 当前代码已强制 | 配置 `JWT_ISSUER` 后严格比对；未配置则拒绝所有 Token（CONFIG_ERROR） |
| Audience 校验 | 当前代码可配置 | 配置 `JWT_AUDIENCE` 后验证；为空跳过（仅限本地开发或显式测试模式，生产推荐必填） |
| JWKS 获取与 `kid` 匹配 | 当前代码可配置 | PyJWKClient 支持 `kid` 匹配和缓存 TTL |
| JWKS 强制 HTTPS | 生产推荐 | 当前代码未强制 URL 协议 |
| Token 最大有效期 | 生产推荐 / 客户待确认 | 当前代码强制校验 `exp`（过期即 401），但未限制最大有效期 |
| Clock skew 容忍 | 尚未实现 | PyJWT 默认 leeway=0s；`JWTValidatorConfig` 未暴露 leeway 配置 |
| 静态公钥文件 | 尚未实现 | 当前仅支持 JWKS URL 或 HMAC 共享密钥 |
| Token revocation | 尚未实现 | 见第 7 节 |
| introspection | 尚未实现 | 见第 7 节 |
| 密钥轮换 | 当前代码可配置 / 客户待确认 | 代码支持 `kid` 匹配；轮换周期与通知方式由客户确认 |

---

## 5. C. 标准 JWT Claims 确认

| Claim | 是否必填 | 字段类型 | 测试默认值 | 客户确认值 | 缺失时 Gateway 行为 |
|---|---|---|---|---|---|
| `iss` | 是 | string | `https://auth.example.com` | **待确认** | 401 `AUTH_TOKEN_INVALID` |
| `aud` | **是（生产）** | string 或 array | `tyrag-gateway` | **待确认** | 生产环境必填并验证；缺失或与 `JWT_AUDIENCE` 不匹配 → 401。`JWT_AUDIENCE` 未配置时跳过校验，仅限本地开发或显式测试模式 |
| `sub` | **是** | string | `biz-user-001` | **待确认** | 401 `AUTH_TOKEN_INVALID`。`sub` 是用户唯一标识，用于 ext_user_map 查找 |
| `exp` | **是** | int (Unix timestamp) | `now + 3600` | **待确认** | 401 `AUTH_TOKEN_EXPIRED` |
| `nbf` | 否 | int (Unix timestamp) | 未设置 | **待确认** | 若存在且未到生效时间则 401 `AUTH_TOKEN_INVALID` |
| `iat` | 否 | int (Unix timestamp) | `now - 60` | **待确认** | 不拒绝请求，但记入 UserPrincipal 供审计 |
| `jti` | 否 | string | 未设置 | **待确认** | 当前不使用，但可用于未来 Token 去重/撤销 |

---
## 6. D. 业务 Claims 确认（核心待填项）

以下是 Gateway 从 JWT 中提取并用于构建 `UserPrincipal` 的业务 claims。
**当前测试使用的字段名仅是占位符，客户必须逐项确认真实字段名和语义。**

| 序号 | Gateway 内部字段 | 当前测试 Claim 名 | 客户真实 Claim 名 | 单值/数组 | 数据类型 | 是否稳定不变 | 权威来源 | 允许为空 | 示例值 | 用户禁用后如何反映 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `tenant_id` | `tenant` | **待确认** | 单值 | string | 是 | **待确认** | 否 | `"customer-a"` | **待确认** |
| 2 | `business_user_id` | `business_user_id` | **待确认** | 单值 | string | 是 | **待确认** | 否 | `"user-001"` | **待确认** |
| 3 | `display_name` | `name` | **待确认** | 单值 | string | 否 | **待确认** | 是 | `"Zhang San"` | **待确认** |
| 4 | `department_ids` | `department` | **待确认** | **数组**（支持单值自动包装） | string[] | 否 | **待确认** | 是 | `["D10","D20"]` | **待确认** |
| 5 | `role_codes` | `roles` | **待确认** | 数组 | string[] | 否 | **待确认** | 是 | `["end_user"]` | **待确认** |
| 6 | `group_ids` | `groups` | **待确认** | 数组 | string[] | 否 | **待确认** | 是 | `["maintenance"]` | **待确认** |
| 7 | `security_level` | `security_level` | **待确认** | 单值 | integer | 否 | **待确认** | 是 | `2` | **待确认** |

### 各字段详细说明

#### 6.1 `tenant_id`（租户 ID）
- **校验规则**: 缺失 → 401。Gateway 不假设默认租户。
- **用途**: 隔离多租户数据；ext_user_map 联合键 (`tenant_id`, `business_subject`)。

#### 6.2 `business_user_id`（业务用户 ID）
- **校验规则**: 缺失时回退为 `sub`。
- **用途**: API 响应中 `businessUserId` 字段。若客户系统中 `sub` 即为业务用户 ID，可配置两者映射到同一 claim。

#### 6.3 `display_name`（显示名称）
- **校验规则**: 缺失时为空字符串，不拒绝访问。
- **用途**: 仅用于 UI 展示和审计日志可读性，不参与权限判断。

#### 6.4 `department_ids`（部门 ID 列表）
- **校验规则**: 缺失时为空列表 `[]`，不拒绝访问。
- **数据格式**: 支持单值字符串（自动包装为单元素元组）或 JSON 数组。
- **用途**: 后续 ACL 防线 B/C 用于部门级数据过滤。

#### 6.5 `role_codes`（角色代码列表）
- **校验规则**: 缺失时为空列表 `[]`。
- **用途**: 派生 `capabilities`。Gateway 当前识别的角色值及对应能力：

| 角色值 | 派生能力 |
|---|---|
| `end_user` | `ask`, `list_sessions`, `view_citations` |
| `knowledge_maintainer` | `upload`, `manage_metadata`, `review` |
| `system_admin` | `admin` |
| `auditor` | `audit` |

- 所有角色默认包含 `read`。
- **最小权限原则**：`role_codes` 缺失、为空或包含未知角色值时，统一只获得最小基础能力 `read`，不自动获得 `ask`/`list_sessions`/`view_citations`；需要这些能力的接口应在后续授权层检查 capability。未知角色与角色缺失不构成不同的提权结果。
- **当前代码行为**：`UserPrincipal._derive_capabilities` 对 `role_codes` 缺失时会额外派生 `ask`/`list_sessions`/`view_citations`，与上述最小权限原则不一致；该行为是否调整需由 WP-01 Phase 1 Review 确认，本任务不修改代码。
- 若客户使用不同角色编码体系（如 `ROLE_OPERATOR`, `ROLE_ADMIN`），需要确认映射关系或在 `JWT_CLAIM_MAP` 中将客户角色 claim 指向 `role_codes` 并配合 Gateway 侧的角色值标准化。

#### 6.6 `group_ids`（用户组 ID 列表）
- **校验规则**: 缺失时为空列表 `[]`。
- **用途**: 后续 ACL 防线 B/C 用于 allow/deny 组过滤。

#### 6.7 `security_level`（安全等级）
- **校验规则**: 缺失或非整数时默认 `0`。
- **取值要求**: 整数。建议在文档中约定范围（如 0-5）。
- **用途**: 后续 ACL 防线 B 中与文档密级比对，用户等级低于文档密级则拒绝。
- **安全默认**: 未确认前不得默认授予高级权限。

---

## 7. E. 用户状态变更通知

| 场景 | 当前 Gateway 支持 | 客户需确认 |
|---|---|---|
| 用户被禁用 | `ext_user_map.status = "disabled"` → 403。注意：契约中尚无独立 disabled 错误码，需由契约负责人确认新增 `AUTH_USER_DISABLED` 或采用其他语义 | 客户如何通知 Gateway 更新 ext_user_map？是否有用户状态变更回调/事件？ |
| 用户离职 | 同上 | 离职是否有独立的 termination 事件，还是等同于禁用？ |
| 部门调整 | 无实时感知（依赖 Token 刷新后 department_ids 变化） | Token 有效期内部门变更是否需要立即生效？如需要，需提供 introspection 或事件通知机制 |
| 角色调整 | 同上（依赖 Token 刷新） | 同部门调整 |
| Token 撤销 | **不支持**。Token 在有效期内始终可用的风险 | 客户是否有 Token Revocation List/Endpoint 或 introspection？若无法撤销，建议缩短 Token 有效期 |

### 需确认的能力

| 能力 | 当前支持 | 客户是否有 | 备注 |
|---|---|---|---|
| Token Revocation (RFC 7009) | 否 | **待确认** | 尚未实现；若客户提供 revocation endpoint，Gateway 需增加验证逻辑 |
| OAuth 2.0 Token Introspection (RFC 7662) | 否 | **待确认** | 尚未实现；若客户提供 introspection endpoint，Gateway 可在每次请求时实时校验 Token 状态 |
| 用户信息查询接口 (UserInfo) | 否 | **待确认** | 尚未实现；用于补充 Token 中未包含的用户属性 |
| 权限变化事件推送 | 否 | **待确认** | 尚未实现；用于主动通知 Gateway 更新 ext_user_map 状态 |
| Token 黑名单/撤销列表 | 否 | **待确认** | 尚未实现；轮询文件或 API 获取已撤销的 `jti` 列表 |

---

## 8. F. 推荐 Token Payload 示例（虚构测试数据）

以下示例使用明显虚构的测试值，仅用于协议对齐讨论。**不得包含真实客户信息。**

```json
{
  "iss": "https://auth.example.test",
  "aud": "enterprise-rag-gateway",
  "sub": "user-test-001",
  "iat": 1754390400,
  "exp": 1754394000,
  "nbf": 1754390400,
  "jti": "jti-test-uuid-0000-0000-0000-000000000001",
  "tenant": "tenant-test",
  "business_user_id": "user-test-001",
  "name": "Test User",
  "department": ["dept-test-d10", "dept-test-d20"],
  "roles": ["end_user"],
  "groups": ["grp-test-maintenance"],
  "security_level": 2
}
```

### Token Header 示例

```json
{
  "alg": "RS256",
  "typ": "JWT",
  "kid": "key-test-2026-001"
}
```

---

## 9. G. Claim Mapping 配置示例

Gateway 通过环境变量 `JWT_CLAIM_MAP` 配置 claim 字段名的映射关系。该变量为 JSON 字符串。

### 当前测试默认映射

```json
{
  "sub": "sub",
  "tenant_id": "tenant",
  "business_user_id": "business_user_id",
  "display_name": "name",
  "department_ids": "department",
  "role_codes": "roles",
  "group_ids": "groups",
  "security_level": "security_level"
}
```

### 映射说明

| JSON key（固定，不可改） | JSON value（可改为客户真实字段名） | 含义 |
|---|---|---|
| `sub` | 客户 Token 中标识用户主题的 claim 名 | 用户唯一标识 |
| `tenant_id` | 客户 Token 中的租户 claim 名 | 租户标识 |
| `business_user_id` | 客户 Token 中的业务用户 ID claim 名 | 业务用户 ID |
| `display_name` | 客户 Token 中的显示名称 claim 名 | 用户显示名 |
| `department_ids` | 客户 Token 中的部门 claim 名 | 部门列表 |
| `role_codes` | 客户 Token 中的角色 claim 名 | 角色列表 |
| `group_ids` | 客户 Token 中的用户组 claim 名 | 用户组列表 |
| `security_level` | 客户 Token 中的安全等级 claim 名 | 安全等级 |

### 假设客户字段名为 `tid`, `dept`, `role`, `sec_lvl` 的配置

```json
{
  "sub": "sub",
  "tenant_id": "tid",
  "business_user_id": "sub",
  "display_name": "preferred_username",
  "department_ids": "dept",
  "role_codes": "role",
  "group_ids": "groups",
  "security_level": "sec_lvl"
}
```

### 所有相关环境变量

| 环境变量 | 用途 | 默认值 | 必填 |
|---|---|---|---|
| `JWT_ISSUER` | 期望的 issuer | 空（不配置则拒绝所有 Token） | **是** |
| `JWT_AUDIENCE` | 期望的 audience；生产必填并验证，用于防止其他系统的 Token 被 Gateway 接受 | 空（跳过校验；仅限本地开发或显式测试模式，非推荐生产配置） | **是（生产）** |
| `JWT_JWKS_URL` | JWKS 公钥端点 URL | 空（不配置且未开启 HS 则拒绝） | **是**（生产） |
| `JWT_ALLOWED_ALGS` | 允许的签名算法（逗号分隔） | `RS256,ES256` | 否 |
| `JWT_ENABLE_HS` | 是否允许 HMAC 算法 | `false` | 否 |
| `JWT_SHARED_SECRET` | HMAC 共享密钥（仅 JWT_ENABLE_HS=true 时使用） | 空 | 条件必填 |
| `JWT_CLAIM_MAP` | Claim 字段名映射 JSON | 见上文默认映射 | 否 |
| `JWT_JWKS_CACHE_TTL` | JWKS 缓存 TTL（秒） | `300` | 否 |
| `JWT_JWKS_TIMEOUT` | JWKS 请求超时（秒） | `5.0` | 否 |

---

## 10. H. Gateway 校验规则

### 10.1 返回 401 Unauthorized 的场景

| 错误码 | 条件 |
|---|---|
| `AUTH_TOKEN_MISSING` | 请求未携带 `Authorization: Bearer <token>` |
| `AUTH_TOKEN_INVALID` | Token 签名无效、算法不允许、issuer 不匹配、audience 不匹配、nbf 未到、sub 缺失、tenant 缺失、JWKS 不可用 |
| `AUTH_TOKEN_EXPIRED` | Token 已过期 (`exp` 早于当前时间) |
| `CONFIG_ERROR` | JWT_ISSUER 未配置、JWKS_URL 未配置且 HS 未开启、无可用算法 |

### 10.2 返回 403 Forbidden 的场景

| 错误码 | 条件 |
|---|---|
| `AUTH_USER_MAPPING_MISSING` | ext_user_map 中无对应映射（映射不存在） |
| `AUTH_USER_DISABLED`（待契约确认） | ext_user_map 存在但 `status = "disabled"`。`contracts/error-codes.yaml` 当前无 `AUTH_USER_DISABLED`，需新增或由契约负责人确认；本任务不修改 error-codes.yaml |

当前代码对 `status = "disabled"` 复用 `AUTH_USER_MAPPING_MISSING`，与“映射不存在”语义混淆；该行为需 WP-01 Phase 1 Review 确认。

### 10.3 参与 UserPrincipal 的 Claims

所有以下 claims 从 JWT 提取后进入 UserPrincipal：

- `sub` → `subject`
- `tenant_id` → `tenant_id`
- `business_user_id` → `business_user_id`
- `display_name` → `display_name`
- `department_ids` → `department_ids`
- `role_codes` → `role_codes` + `capabilities`（派生）
- `group_ids` → `group_ids`
- `security_level` → `security_level`
- `iat` → `token_issued_at`
- `exp` → `token_expires_at`

### 10.4 参与后续 ACL 的 Claims（WP-01 Phase 2：ACL 与资源授权阶段生效）

- `tenant_id`
- `department_ids`
- `role_codes` → `capabilities`
- `group_ids`
- `security_level`

### 10.5 身份不可覆盖原则

以下字段**绝不以任何形式**从请求正文中读取：

- `tenant_id`
- `business_user_id`
- `department_ids`
- `role_codes`
- `security_level`

即使用户在 API 请求体中传递这些字段，Gateway 也会忽略，仅使用 JWT claims 中的值。

---

## 11. I. 未确认项的系统行为（安全默认）

| 未确认项 | 当前行为 | 安全影响 |
|---|---|---|
| `security_level` 取值未确认 | 缺失时默认 `0`；不拒绝任何等级的文档访问（防线 B 尚未实施） | 防线 B 实施后：文档密级高于用户 `security_level` 将拒绝。默认 `0` 意味着只能访问无密级文档 |
| `department_ids` 缺失 | 默认为空列表 | 防线 B 实施后：基于部门的过滤将不会命中任何部门 → 可能无结果返回。是采用**拒绝**还是**最小权限**（仅公开文档）取决于后续 ACL 策略决策 |
| `role_codes` 缺失或为空 | 当前代码会派生 `ask`/`list_sessions`/`view_citations`（待 WP-01 Phase 1 Review 确认）；文档目标行为为仅最小基础能力 `read` | 需要这些能力的接口必须由后续授权层检查 capability，不得因角色缺失自动放行 |
| `role_codes` 包含未知值 | 仅获得 `read`，不报错，不赋权 | 不会将无法识别的角色映射为管理权限；与角色缺失统一为最小权限，无不同提权结果 |
| `group_ids` 缺失 | 默认为空列表 | 防线 B 实施后：deny 规则优先于 allow，空 group 可能没有匹配任何 allow 规则而被拒绝 |
| JWKS URL 不可用 | fail closed（当前代码已强制）— 返回 401 `AUTH_TOKEN_INVALID` | 不会在无法验证签名时放行请求 |
| 未知角色代码 | 不自动赋权，仅获得 `read` | 不会将无法识别的客户角色映射为管理权限 |
| Token 有效期过长 | 当前代码仅校验 `exp`，未限制最大有效期 | 建议客户将 Access Token 有效期控制在 15-60 分钟（生产推荐，待客户确认） |

---

## 12. J. 阻断正式上线的未决项

以下项目必须在生产上线前由客户确认，否则存在功能缺口或安全风险：

| 序号 | 未决项 | 风险等级 | 说明 |
|---|---|---|---|
| 1 | **业务 claims 真实字段名** | 高 | 当前测试默认值 (`tenant`, `department`, `roles` 等) 不能作为正式协议 |
| 2 | **签名算法和 JWKS URL** | 高 | 无 JWKS 则无法在生产环境验证签名；生产推荐 JWKS 使用 HTTPS（当前代码未强制） |
| 3 | **Issuer 值** | 高 | 无 issuer 配置 Gateway 拒绝所有 Token |
| 4 | **Token 最大有效期** | 中 | 当前代码仅校验 `exp`，未限制最大有效期；过长有效期增加泄露风险，且无撤销机制 |
| 5 | **角色编码体系** | 中 | 客户角色如何映射到 Gateway 的 `end_user`/`knowledge_maintainer`/`system_admin`/`auditor` |
| 6 | **`security_level` 取值范围** | 中 | 需与文档密级对齐，建议统一为 0-5 整数量表 |
| 7 | **用户状态变更通知机制** | 中 | 当前只能通过修改 ext_user_map 数据库行来禁用用户，无自动化途径；disabled 错误码需契约负责人确认 |
| 8 | **Token 撤销机制** | 中 | Token 在有效期内始终可用，无法主动撤销 |
| 9 | **部门/角色变更的生效时效** | 低 | 当前依赖 Token 自然过期后刷新，客户是否接受此延迟 |
| 10 | **用户离职处理** | 低 | 是否等同于禁用？需要客户确认业务语义 |
| 11 | **PostgreSQL 用户映射存储** | 高 | 当前为 SQLite；PostgreSQL Repository、迁移和连接池尚未实施，生产上线前必须完成迁移和并发验证 |

---

## 附录 A. 术语对照

| Gateway 术语 | 说明 |
|---|---|
| `UserPrincipal` | 从已验证 JWT claims 构建的用户身份主体对象 |
| `claim_map` / `JWT_CLAIM_MAP` | 配置化的字段名映射，将客户 JWT claim 名映射到 Gateway 内部字段名 |
| `ext_user_map` | 外部用户映射表，记录 `(tenant_id, business_subject)` 到 RAGFlow 用户的对应关系及状态 |
| `capabilities` | 从角色派生的能力集合（如 `ask`, `admin`, `audit`），用于后续 ACL 判断 |
| fail closed | 安全策略：当外部依赖（如 JWKS）不可用时拒绝请求，而非放行 |

## 附录 B. 相关文件清单

| 文件 | 用途 |
|---|---|
| `enterprise/gateway/auth/token_validator.py` | JWT 验证核心逻辑 |
| `enterprise/gateway/auth/user_principal.py` | 用户身份主体 |
| `enterprise/gateway/auth/middleware.py` | 认证中间件依赖 |
| `enterprise/gateway/config.py` | 集中配置 |
| `enterprise/gateway/models/ext_user_map.py` | 用户映射持久层 |
| `enterprise/gateway/auth/CHANGE-REQUEST-WP01A-CLAIMS.md` | Claims 未决项记录 |
| `enterprise/tests/test_wp01a.py` | 单元和契约测试 |
| `contracts/integration-openapi.yaml` | API 契约（UserPrincipal schema） |
| `contracts/error-codes.yaml` | 错误码定义 |
| `docs/04-身份SSO-RBAC与ACL.md` | 身份方案设计文档 |

## 附录 C. 后续步骤

1. 客户设备管理系统开发人员填写本文档中所有 **"待确认"** 项
2. 双方对齐角色编码映射表（客户角色 → Gateway 角色）
3. Gateway 团队根据确认结果更新 `JWT_CLAIM_MAP` 和其他环境变量
4. 在测试环境用客户签发的真实格式 Token 进行集成测试
5. 确认用户状态变更通知的集成方式
6. 确认后关闭 `CHANGE-REQUEST-WP01A-CLAIMS.md`

---

*本文档由 Enterprise RAG Gateway 团队基于 WP-01A 代码实现编写。文中所有示例值均为虚构测试数据。*
