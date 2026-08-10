# TYrag P0 External Integration Contract Rebaseline v2 与 Enterprise 自动化验收计划

## 1. 目标与成功标准

在基线 `8e877950636cecd8e4150344400d494389fecde7` 上新增 `/enterprise/api/v2`，保持 v1 wire 兼容，形成设备管理系统与 TYrag 的 v2.0.0 集成候选基线。候选不是生产验收；生产 readiness 仍要求真实 Asset Registry/RAGFlow/Redis 环境和后续 Gateway PostgreSQL repository。

1. `contracts/integration-openapi-v2.yaml` 可解析，所有 operationId 唯一，所有操作具有真实 `x-status`。
2. v2 外部 DTO 不暴露 RAGFlow dataset/document/chat/session/message 等内部标识。
3. 文档鉴权、tenant/source 成对绑定、HMAC、Redis/Valkey 原子防重放和四类幂等冲突均有自动化测试；生产 Redis 缺失时 fail closed。
4. 会话列表、Asset Registry context/5 分钟 snapshot TTL/immutable equipment、检索过滤、durable run 幂等、真 SSE/JSON、归档、suggestions、citation snapshot 均有自动化测试。
5. `enterprise/scripts/run_enterprise_tests.ps1 -Profile P0` 零 skip/xfail/xpass 并返回 0。
6. 验收前后 `git status --short -- ragflow` 一致且为空。

## 2. 契约与版本策略

- v1：保留现有 wire 行为，只接受兼容修复和安全修复。
- v2：新增 `/enterprise/api/v2`，使用 `contracts/integration-openapi-v2.yaml`。
- v1 不得删除；删除前必须具备消费者清单、独立 ADR 和至少 90 天迁移通知。
- v2 JSON envelope 使用 camelCase；`metadata` 保持 canonical snake_case，并在边界校验 envelope/metadata 等值字段。

## 3. 工作包

| ID | 工作 | Owner | 依赖 | 复杂度 | 可并行 | 验收 |
|---|---|---|---|---|---|---|
| T0 | 冻结文档、ADR、错误码、v2 OpenAPI | Lead | 无 | 中 | 否 | Contract gate C-01..C-20 |
| T1 | CredentialIdentity、HMAC、rotation/replay | Identity/ACL | T0 的字段决策 | 中 | 是 | E-01..E-03、E-16 |
| T2 | 文档 v2 router 与幂等冲突 | File Sync | T0 的文档契约 | 中 | 是 | E-04..E-06 |
| T3 | 会话 v2 router/store/retrieval | Retrieval | T0 的会话契约 | 高 | 是 | E-07..E-14、E-16 |
| T4 | 自动化 runner、静态契约测试、报告 | QA | T0 文件名 | 中 | 是 | P0/Integration profiles |
| T5 | README/docs/WP-05 定位校正 | Lead/Frontend | T0 | 低 | 是 | 全仓描述扫描无冲突 |
| T6 | 装配、全量回归和验收报告 | Lead + QA | T1..T5 | 高 | 否 | 三层 gate 全部通过 |

## 4. P0 / P1 / P2

- P0：文档 ingestion/status/lifecycle、ServiceAuth tenant/source binding、HMAC rotation/replay、幂等冲突、会话 list/detail/messages/context/archive、context 检索过滤、clientMessageId、规则 suggestions、server-controlled prompt、strict DTO、citation snapshots。
- P1：outbound callback、transient conversation attachment、只读 Business PG/TimeSeries adapter、hybrid fusion。
- P2：LLM dynamic suggestions、advanced memory、advanced agent behavior。

## 5. 自动化 profiles

- `Contract`：YAML/JSON、operationId、`x-status`、strict schema、无外部内部 ID、跨文档一致性。
- `P0`（默认）：Contract + 离线 Python + TypeScript noEmit + Vitest；明确排除 live 测试。
- `Integration`：P0 + 在线 RAGFlow public API contract + Asset Registry resolver + WP04 E2E + Redis cross-instance replay；环境缺失必须返回 exit 3，不得 skip 或伪造通过。
- `All`：Integration + P1 测试；P1 未实现时允许该 profile 失败，但不阻断 P0 冻结。

## 6. 验收保证

### Contract Gate

- C-01 OpenAPI/JSON parse；C-02 operationId 唯一；C-03 `x-status` 合法且真实；C-04 P0 路由齐全；C-05 P1 为 planned；C-06 无外部 RAGFlow ID；C-07 request schema strict；C-08 camelCase/metadata snake_case 边界；C-09 credential binding；C-10 rotation；C-11 replay；C-12 idempotency A-D；C-13 duplicate=202；C-14 contextVersion；C-15 retrieval 交集；C-16 clientMessageId；C-17 SSE/JSON；C-18 suggestions stale；C-19 state/citations 解耦；C-20 无 P0 open question。

### Runtime Gate

- E-01 valid HMAC；E-02 tenant/source 越权拒绝；E-03 stale/replay/revoked 拒绝；E-04 文档首次 202；E-05 replay/dedupe；E-06 conflict 409；E-07 创建/list cursor；E-08 context patch/version/conflict；E-09 retrieval context filter；E-10 strict fields 422；E-11 clientMessageId replay/conflict；E-12 SSE/JSON；E-13 messages cursor/archive；E-14 suggestions stale；E-15 citation external snapshot；E-16 所有权限负向用例。

### 完成判定

- 只通过 Contract Gate 时仅可声明 `CONTRACT FROZEN`，不可声明实现完成。
- 当前提交只能声明 `INTEGRATED_CANDIDATE_BASELINE`；`P0_IMPLEMENTATION_ACCEPTED` 还必须包含真实 Redis cross-instance replay 证据，并由 Lead 在最后一次写入后重跑 P0。
- `Integration` profile 返回 0、在线 public API/E2E 通过且 `ragflow/` 无变化后，方可声明 `INTEGRATION_ACCEPTED`。

## 7. 风险与回滚

- v2 以独立 router/store 增量装配，回滚为移除 v2 router include，不触碰 v1 数据和 wire。
- SQLite 仅为当前 Enterprise 开发基线；生产迁移由 Lead 单独管理，本任务不修改 RAGFlow 官方迁移。
- 在线 RAGFlow 或 Asset Registry 不可用只影响 Integration Gate（exit 3），不得降低 P0 离线门禁；Redis replay 集成测试缺失不得用 memory/mock 替代。
- callback 与 attachments 仅冻结 P1，不得伪标 implemented。
