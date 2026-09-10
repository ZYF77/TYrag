# ADR-018 Gateway 授权范围与 Metadata Restrict

- 状态：Accepted
- 日期：2026-09-09

## 背景

Gateway 已经负责租户 ACL、文档可检索状态、每轮 doc_ids 和设备实体解析。
RAGFlow 负责问题理解、metadata 条件生成、检索和回答。现有设备策略会在普通追问中
把候选资料默认收窄到活动设备，且普通聊天的 metadata 过滤发生在可选的多轮问题重写
之前。官方 metadata helper 的默认语义是 extend（并集），不能直接改成企业专用
交集语义。

## 决策

1. Gateway 增加 ENTERPRISE_RETRIEVAL_SCOPE_POLICY。默认 legacy_device，保留当前设备
   收窄；authorized_context 只改变默认设备收窄决策，ACL、文档 readiness、设备解析和
   最终硬 doc_ids=G 仍由 Gateway 负责。显式设备线索无法解析时继续 fail closed。
Gateway Console 运行时可热更新检索范围策略（retrievalScope.policy），默认仍为 legacy_device；非法值回退到 legacy_device。

2. Gateway 将本轮策略、doc_scope_mode、context_version 和有限业务上下文写入
   ext_v2_message_run.retrieval_context_json。重试和回放使用这个快照，不重新读取已变化
   的配置。会话的型号、设备类型和制造商作为增量字段持久化。
3. RAGFlow 的公共 apply_meta_data_filter 默认保持官方 extend 行为。只有 Gateway
   传入 doc_scope_mode=restrict 时，metadata loader、ES/Infinity pushdown 和内存
   fallback 才限制在 Gateway 的 G 内，并把结果与 G 求交。
4. restrict 的空集合契约固定为：None、[] 和旧哨兵 ["-999"] 都产生空证据；有效条件零
   命中产生 []，禁止回退到全库或未过滤检索。显式空 scope 在简单、Agentic 及工具继承
   路径都不能被补回。
5. 普通聊天和 Agentic 都先完成问题重写，再运行 metadata 条件过滤。软业务上下文只
   用于问题理解和提示词身份说明，不构成授权条件或证据；文档事实以每份资料的
   document_metadata.equipment_id / fixed_asset_no 为准。
6. 现有 Evidence Contract、Grounding、引用投影和最终硬范围过滤继续保留。本 ADR
   不增加 Gateway LLM、运行时 EAM 查询、Evidence Gate、boost 或多路融合；没有证据
   表明 async_ask 和独立 mindmap 入口经由此 Gateway，暂不改动。

## 影响

- authorized_context 会扩大候选资料，可能增加噪声和延迟；需要灰度比较串设备率、
  空命中率和拒答率。
- model、equipment_type 和 manufacturer 必须在文档 metadata 中可信存在。缺失时模型
  不能把会话字段当作文档事实。
- ragflow/** 的改动需要构建新镜像；仅重启旧镜像不会生效。

## 发布与回滚

先保持 legacy_device，完成离线回归、跨租户/空集负例和真实目标的灰度验证，再按租户
切换 authorized_context。回滚时将 Gateway 开关恢复为 legacy_device，并使用移除
RF-PATCH-011 的 RAGFlow 镜像；数据库新增列保留，不影响旧运行记录读取。
