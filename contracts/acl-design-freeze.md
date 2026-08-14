# ACL Design Freeze

- 状态：Frozen
- 日期：2026-08-05（部门硬规则于 2026-08-13 经 ADR 放宽）
- 基线：`d12f0a2` / RAGFlow `v0.26.4`
- 策略版本：`ACL_POLICY_VERSION=1.1`
- 范围：WP-01 Phase 2 M1/M2（AclScope、compile_scope、document ACL 决策）

## 1. 冻结规则

以下规则在 P0 Stabilization 中冻结，任何放宽都必须先创建 ADR 并补负向测试。

| 规则 | 条件 | 结果 |
|---|---|---|
| 租户 | 文档 tenant 缺失或不等于 principal tenant | deny |
| 文档状态 | `business_status != active` | deny |
| deny group | principal group 命中文档 deny group | deny（优先于 allow） |
| security level | 文档 security_level 缺失 | UNRESOLVED → deny |
| security level | principal 等级低于文档等级 | deny |
| allow group | 文档 allow_group_ids 为空 | UNRESOLVED → deny |
| allow group | 文档有 allow 规则但 principal 无命中 | deny |

部门（JWT `department` / 文档 `department_id`）**不参与硬决策**。空值或不匹配不得单独 deny。问询范围由会话 `equipmentId` 与上表规则约束。见 [`docs/adr/acl-department-not-hard-deny.md`](../docs/adr/acl-department-not-hard-deny.md)。

## 2. 已收回的 PENDING 开关

- `admin_bypass_document_acl`：已从策略中删除，任何角色不得绕过文档 ACL。
- `empty_allow_groups_public`：已从策略中删除，空 allow 组不构成公开访问。

## 3. Scope fail-closed 约束

- materialized scope 的 `document_ids` 为空时，scope 必须为 empty。
- metadata predicate 的 `manual` 条件为空、方法不是 `manual` 或 logic 不在 `and/or` 时，scope 必须为 empty。
- `compile_scope` 必须校验 resolver 返回类型和 scope 有效性；非 `AclScope`、无效模式或无效 predicate 一律返回 empty。
- 没有安全 predicate 时调用方必须 short-circuit，不执行任何检索。

## 4. 对外行为

所有决策对外只暴露 `allowed`。`UNRESOLVED` 的 `allowed` 必须为 `False`，
任何调用方不得把 UNRESOLVED 解释为允许或 public。

## 5. 实现位置

- `enterprise/gateway/acl/schema.py`：AclScope 与文档 ACL facts；
- `enterprise/gateway/acl/scope.py`：compile_scope 与 resolver 校验；
- `enterprise/gateway/acl/policy.py`：document ACL 决策；
- `enterprise/tests/test_acl_core.py`：冻结规则与 fail-closed 测试。
