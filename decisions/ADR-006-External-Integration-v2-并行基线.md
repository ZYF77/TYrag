# ADR-006：External Integration 使用并行 `/v2` 基线

- 状态：Candidate baseline（wire contract v2.0.0；非生产验收）
- 日期：2026-08-09
- 基线：`8e877950636cecd8e4150344400d494389fecde7`

## 背景

仓库中的 v1 已声明为 `1.0.0` 并在响应中公开 `ragflowDatasetId`、`ragflowDocumentId` 等内部标识。仓库没有证据证明设备管理系统已实际消费 v1，也没有足够证据证明可以安全破坏它。

## 决策

采用独立 `/enterprise/api/v2`。v1 保持 wire 兼容，仅接受兼容和安全修复；新消费者只接入 v2。v2 禁止公开 RAGFlow 内部标识，并以 Enterprise 数据库作为会话历史、消息状态和 citation snapshot 的外部事实源；RAGFlow session 只作为上下文执行引擎。

v1 退役必须另行满足：消费者清单完整、迁移验证通过、独立 ADR、至少 90 天通知。未知消费者不视为“没有消费者”。

v2 的 equipmentId 唯一由设备管理系统 Asset Registry 解析；文档 metadata 只能作为非权威参考。Gateway 持久化 registry snapshot，5 分钟 TTL 惰性刷新；首条消息后 canonical equipment 不可变，设备切换必须新会话。消息端点保持单一路径并按 `Accept: text/event-stream` 直接转发 RAGFlow 真流式，不能用完整答案伪造 delta。

HMAC replay protection 使用独立 Redis/Valkey 逻辑空间的原子 `SET NX EX 600`。生产 Redis 不可用时 fail closed，内存实现仅允许显式测试模式。消息幂等使用 durable run 状态机和有限租约；重复 pending 请求返回 202 相同 runId，不自动重跑；租约过期固定为 `RUN_INTERRUPTED`。

本候选仍以 Enterprise 自有 SQLite 承载 Gateway 状态，目标为单 Gateway/多 worker。生产多副本 PostgreSQL repository、Asset Registry/RAGFlow 实环境和跨实例 Redis 验证均是后续 production readiness gate；不得使用客户业务 PG 或 RAGFlow 官方 DB 承载 Gateway 状态。

## 影响

- 服务同时装配 v1/v2 router，增加有限维护成本。
- 设备管理系统需要按 v2 契约接入。
- v1 的内部字段不会复制到 v2。
- 回滚 v2 不要求回滚或改写 v1 数据。

## 未选择方案

- Direct Rebaseline：无法证明破坏安全。
- v1 仅字段 deprecated：无法解决严格 DTO、路径和幂等语义的整体变化。
