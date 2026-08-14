# ADR：文档 ACL 不再将 department 作为硬 deny

- 状态：Accepted
- 日期：2026-08-13
- 决策人：Identity/ACL + Retrieval

## 背景

冻结规则把 JWT `department` 与投喂 `metadata.department_id` 做硬匹配：任一方为空则 `UNRESOLVED` deny，双方存在但不匹配则 `DEPARTMENT_DENIED`。EAM 联调中这导致合法问询返回 `no_reliable_evidence` 且不进入 RAGFlow。

两个字段不是同一语义：

- JWT `department`：当前登录用户的组织部门（`Sys_User.DeptId`）。admin 可能为空或与设备部门不同。
- 投喂 `department_id`：设备/文档归属部门。

问询会话已经用 `equipmentId` / `fixedAssetNo` 限制检索范围。再要求「用户部门号等于设备部门号」与 EAM「能打开这台设备就能问」不一致。

## 约束

- ACL Design Freeze 放宽必须有 ADR 和负向测试。
- 对外 HTTP / JWT / 投喂 JSON 形状不变。
- 不得用管理员角色绕过文档 ACL。
- 租户隔离、文档状态、deny/allow 组、密级、设备上下文必须保留。

## 备选方案

1. **继续硬 deny**：EAM 必须伪造部门或只让同部门用户提问。否决。
2. **匹配优先、否则回退同设备文档**：安全收益小，实现重。否决（可日后做排序，不进硬规则）。
3. **部门退出硬规则**：保留租户、组、密级、设备范围。采纳。

## 决策

`evaluate_document_acl` 不再检查 department。空部门、部门不匹配，在其它冻结规则通过时一律允许。`ACL_POLICY_VERSION` 升为 `1.1`。

JWT 仍可携带 `department`；投喂仍写入 `department_id`。二者不再决定问询能否检索。

整数形式的 `department` claim（如 `2`）在解析时收成字符串列表，避免 .NET 单值序列化被当成未传。这不改变硬规则，只修正 claim 形状。

## 正面影响

- 跨部门查看本设备资料不再被 ACL 空范围短路。
- 联调不再要求用户 DeptId 等于设备部门号。
- 接口契约不变，EAM 客户端不用发版。

## 负面影响和风险

- 同一租户内、组与密级通过的用户，可以看到会话设备下其它部门归属的文档。设备上下文仍是范围边界。
- 若未来需要部门级隔离，应另做明确授权模型，而不是把用户 DeptId 和文档归属部门当成同一 claim。

## 验证方式

- `test_acl_core`：空部门、部门不匹配在其它规则通过时 `allowed=True`；租户/组/密级负向用例仍 deny。
- v2 问询：JWT `department=["3"]` 仍检索 `department_id=2` 的同设备文档并进入 RAGFlow stub。
- 30 环境：EAM `admin`（部门 `3` 或空）问 `GI01240023` 应进入 RAGFlow。

## 回滚方式

恢复 `policy.py` 中部门空值 / 不匹配 deny，将 `ACL_POLICY_VERSION` 改回 `1`，并回滚对应测试与文档。

## 对上游升级的影响

无。只改 Enterprise Gateway ACL 策略，不改 RAGFlow 核心。
