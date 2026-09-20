# ADR-021：Agent Workflow 受控范围与多格式 Pipeline 输入

## 状态

Accepted，2026-09-14。

## 背景

TYrag 新增 Agent Workflow 测试入口。画布原生 Retrieval 可以读取 Dataset 和
Metadata Filter，但没有企业 Gateway 授权文档集合的硬范围；仅把文档 ID 放进
Begin 提示词不能形成权限控制。现有 EAM 也已经使用 JSON/JSONL 投喂，Pipeline
Parser 原先没有 JSON 配置分支。

## 决策

1. Retrieval 增加可选 `doc_scope_ids` 和 `doc_scope_mode` 参数。`restrict` 模式
   只接受 Gateway 注入的文档集合；集合为空时返回正常空结果，不回退到全库。
2. Metadata Filter 只能在该集合内进一步缩小，不能扩展；父子块等后续读取继续
   使用同一文档边界。
3. Parser 增加 JSON/JSONL/LDJSON 配置分支，调用上游已有 `JsonParser`，保留对象、
   数组和记录边界。普通 Parser 默认行为不改变。
4. Gateway Workflow 代理负责注入 scope、会话、附件和用户 Memory；模型不能修改
   这些安全输入。
5. `restrict` Retrieval 的有效范围定义为 `S = G ∩ metadata_filter`。当 metadata
   条件有效但 S 为空时，Retrieval 必须在调用检索器前返回空输出；不得依赖下游 ES
   对空列表的处理。需要放宽设备条件时，必须由 Workflow 显式调用另一个仍绑定 G
   的补检索工具，不能由空 `doc_ids` 隐式回退。

## 不在本 ADR 内

ACL 规则、Asset Registry、文档质量门、用户 Memory 主体、公共 Query 契约、
Citation 映射和 30 部署流程不由这两个 RAGFlow 参数取代，继续遵循现有 ADR。

## 回滚

停止 Workflow 测试入口并使用旧 RAGFlow 镜像即可；普通 Chat 和原有 Parser 配置
不依赖新参数。上线前必须完成范围外文档、空范围、JSON/JSONL 以及跨租户负例。
