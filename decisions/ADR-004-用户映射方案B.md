# ADR-004 用户映射方案 B（Gateway 服务代理）

- 状态：Accepted
- 日期：2026-08-05
- 决策人：Enterprise Integration Lead
- 关联基线：`d12f0a2` / RAGFlow `v0.26.4` (`cb93883f3f8c975eecb2fed81210effeb3bdb06f`)

## 背景

企业用户从客户系统 SSO 进入，RAGFlow 只承担解析、索引、检索和问答能力。
方案 A 为每个业务用户创建独立 RAGFlow 用户/租户，方案 C 按部门/角色创建受限服务身份。
公开 API 中缺少可管理的用户生命周期能力，且业务权限权威在客户侧，不适合把企业用户逐人复制到 RAGFlow。

## 约束

- 不修改 RAGFlow 内部数据库或官方模型；
- 不使用客户 Token 或业务主体直接调用 RAGFlow；
- 业务用户、部门、角色和 ACL 的权威来源保留在客户系统/业务 PostgreSQL；
- 权限必须由 Gateway 在召回前过滤和返回前复核执行。

## 备选方案

1. 方案 A：每个业务用户映射独立 RAGFlow 用户和租户；受公开 API 用户生命周期能力限制，维护成本高。
2. 方案 C：按部门/角色建立有限服务身份；身份数量仍随组织增长，且部门/角色变化需要服务身份生命周期管理。

## 决策

采用方案 B：Gateway 作为 RAGFlow 的服务代理，使用单一企业服务身份/API Key，
通过 `ext_user_map` 保存 `(tenant_id, business_subject)` 到业务用户映射，
RAGFlow 侧按租户创建 `enterprise-{tenant_id}` dataset。
业务用户的最小权限和能力（`read`，明确 `end_user` 时增加 `ask` 等）由 Gateway 派生，
客户 JWT 字段名通过 `JWT_CLAIM_MAP` 配置，不硬编码。

## 正面影响

- 不依赖 RAGFlow 用户注册/禁用等未冻结公开 API；
- 身份和 ACL 逻辑集中在企业层，便于独立测试和升级；
- 业务用户数量变化不影响 RAGFlow 侧账号生命周期。

## 负面影响和风险

- 所有业务用户共享 RAGFlow 服务身份，必须由 Gateway 严格执行租户、部门和文档 ACL；
- 一旦 Gateway 出现 fail-open，泄漏面是整个企业租户；
- RAGFlow 侧审计无法直接区分业务用户，需要 Gateway 记录 `requestId`、业务主体和操作。

## 验证方式

- `pytest enterprise/tests -q` 覆盖 WP-00/WP-01/WP-02A/ACL/P0 契约；
- Docker 可用时运行 `enterprise/tests/validate_mapping_strategies.py` 产出真实对比矩阵；
- ACL 负向用例覆盖越权、空规则、disabled 用户和角色缺失场景。

## 回滚方式

本决策不修改 RAGFlow 上游；回滚只需调整 Gateway 部署配置和用户映射策略，
不影响 RAGFlow 数据迁移。

## 对上游升级的影响

方案 B 通过公开 API 使用 RAGFlow，升级时优先检查 dataset/document/chat API 契约；
不依赖内部数据库结构，因此不受上游用户表迁移直接影响。
