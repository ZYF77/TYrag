# WP-01A 条件检查点

**日期:** 2026-08-05
**基线:** `d12f0a2` — P0 Stabilization Review & Fix
**结论:** P0 STABILIZED PASS
**组合测试:** 162/162 Python tests passed（frontend tests/build 另行记录）

---

## 已完成

| 模块 | 文件 | 状态 |
|---|---|---|
| UserPrincipal | `enterprise/gateway/auth/user_principal.py` | 稳定 |
| JWTValidator (JWKS/HS) | `enterprise/gateway/auth/token_validator.py` | 稳定 |
| require_user_principal | `enterprise/gateway/auth/middleware.py` | 稳定，与 ServicePrincipal 分离 |
| ext_user_map | `enterprise/gateway/models/ext_user_map.py` | 持久连接，唯一约束，nullable ragflow_user_id |
| GET /auth/me | `enterprise/gateway/app.py` | 与 OpenAPI 一致 |
| OpenAPI 契约 | `contracts/integration-openapi.yaml` | 已更新 |
| JWT 配置 | `enterprise/gateway/config.py` | 可配置 claim mapping |
| ACL fail-closed | `enterprise/gateway/acl/schema.py` + `scope.py` | 空 materialized/document、空 manual、resolver 输出校验 |
| ACL 策略冻结 | `enterprise/gateway/acl/policy.py` | 删除 admin bypass / empty allow public；UNRESOLVED 一律 deny |
| 最小权限 | `enterprise/gateway/auth/user_principal.py` | role_codes 缺失/未知仅 read |
| 禁用错误码 | `enterprise/gateway/auth/middleware.py` | `AUTH_USER_DISABLED` |
| 错误安全 | `enterprise/gateway/app.py` + `sync/ragflow_document_client.py` | 稳定 code/safe message/requestId，RAGFlow 原始正文仅脱敏日志 |
| WP-01A 测试 | `enterprise/tests/test_wp01a.py` | 41 项 (JWT×12, Principal×10, Persistence×7, API×6, Regression×4, Concurrency×2) |
| WP-02A 回归 | `enterprise/tests/test_wp02a.py` + `test_wp02a_closure.py` | 46/46 通过 |
| ACL 冻结回归 | `enterprise/tests/test_acl_core.py` | 39 项 |
| P0 回归 | `enterprise/tests/test_p0_stabilization.py` | 10 项：错误安全、日志脱敏、404 契约、OpenAPI/error-codes/metadata 契约 |
| Phase 0 spike | `enterprise/tests/validate_mapping_strategies.py` | 脚本就绪，Docker 不可用时 skip |

---

## Pending

### 1. Docker mapping strategy validation
- **文件:** `enterprise/tests/validate_mapping_strategies.py`
- **命令:** `pytest enterprise/tests/validate_mapping_strategies.py -v`
- **前置:** Docker 可用，RAGFlow 运行在 `ENTERPRISE_RAGFLOW_BASE_URL`，配置 `ENTERPRISE_RAGFLOW_API_KEY`
- **输出:** `artifacts/mapping-strategy-comparison.json`
- **阻塞:** 方案 A/B/C 未产生真实对比矩阵前，映射策略未锁定

### 2. P0 契约校准已完成
- POST `/documents` 实际返回 202，OpenAPI 同步为 202；
- `GET /documents/{id}/status` 的真实查询参数 `tenant_id`、`refresh` 已写入 OpenAPI；
- 未实现端点（disable、conversations、messages:stream、citations、sync-status）标记为 `x-status: planned`；
- frontend 依赖的 `/documents/sync-status` 已在 OpenAPI 标记 planned，不伪装为已实现。

### 3. 确认客户 JWT claims
- **文件:** `enterprise/gateway/auth/CHANGE-REQUEST-WP01A-CLAIMS.md`
- **未决字段:** `tenant` claim 名、`department` 单值/数组及字段名、`roles` 值映射、`groups` 字段名、`security_level` 取值范围、`display_name` 字段名、`business_user_id` 是否独立于 `sub`、Token 过期窗口
- **当前默认映射:** 见 `JWT_CLAIM_MAP` 环境变量，可配置，不硬编码
- **阻塞:** 客户未确认前使用默认映射，确认后更新部署配置，无需代码变更

---

## 未修改

- 所有 RAGFlow 上游源码 (`ragflow/`)
- RAGFlow 数据库模型和迁移
- WP-02A 核心逻辑 (`sync/*`, `health.py`, `ragflow_client.py`, `source_adapter.py`)
- `enterprise/gateway/auth/service_auth.py` / `service_principal.py`

---

## 禁止项确认

- 无 ACL compiler
- 无 citation verifier
- 无 RAGFlow metadata filter
- 无 Chat/Session/PDF 全资源 IDOR 测试
- 无 /auth/verify
- 无 admin user-mapping CRUD
- 无 WP-01B
- 无 RAGFlow DB 写入
- 无密钥/Token 提交

---

## 进入 WP-01B 前必须完成

1. Docker 可用时执行 Phase 0 spike，产出 `artifacts/mapping-strategy-comparison.json`
2. 根据 spike 结果确定最终映射策略（方案 B 为当前默认推荐）
3. WP-02A owner 确认 dedup 修复
4. 客户确认 JWT claims 映射或显式接受默认值（最小权限行为已冻结：缺失/未知 role 仅 read）
