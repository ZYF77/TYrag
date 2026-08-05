# CHANGE-REQUEST-WP01A-CLAIMS — 客户 JWT Claims 未决项

状态: Open
日期: 2026-08-05

## 摘要

以下 JWT claims 的字段名称和语义尚未与客户确认。
当前实现使用可配置的 `JWT_CLAIM_MAP` 环境变量，
不硬编码任何客户字段名。

## 未决 Claims

| Claim | 默认字段名 | 配置键 | 问题 |
|---|---|---|---|
| 租户 ID | `tenant` | `tenant_id` | 客户是否使用 `tenant` 还是 `tenant_id`、`tid`？ |
| 部门 | `department` | `department_ids` | 单值还是数组？字段名是 `department` 还是 `departments`、`dept`？ |
| 角色 | `roles` | `role_codes` | 字段名和角色值对应关系？需映射为 `end_user`、`knowledge_maintainer`、`system_admin`、`auditor` 四种 |
| 用户组 | `groups` | `group_ids` | 字段名？用于 ACL 允许/拒绝 |
| 安全等级 | `security_level` | `security_level` | 取值范围和语义？整数等级还是标签？ |
| 显示名称 | `name` | `display_name` | 字段名是 `name` 还是 `display_name`、`preferred_username`？ |
| 业务用户 ID | `business_user_id` | `business_user_id` | 是否独立于 `sub`？若 `sub` 已是业务用户 ID，可复用 |
| Token 过期 | `exp` | — | 标准 JWT claim，无需映射。过期窗口多大？ |

## 默认映射

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

## 验证方式

所有实现使用 `UserPrincipal.from_validated_claims(claims, claim_map)`，
claim_map 来自 `JWT_CLAIM_MAP` 环境变量。
不硬编码。

## 待确认后更新

确认后更新 `JWT_CLAIM_MAP` 部署配置，不需要代码变更。
