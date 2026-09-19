# TYrag Agent Workflow 第二测试入口与多格式 Ingestion Pipeline v1

> Companion docs: `enterprise-qa-agent-workflow-v1.md`, `enterprise-ingestion-pipeline-v1.md`.
> 状态：本地实现中，30 联调机暂不部署。30 上的 RAGFlow CPU 正在执行解析识别作业；待作业结束并收到单独部署授权后，才执行部署与真实调优。
>
> 本文保留 Agent Workflow 与 Ingestion Pipeline 的完整设计，并补充当前未提交 EAM 用户 Memory 的接入约束。文中的配置模板可审查、可导入，但在替换模型占位符和配置固定 Workflow 版本前，不代表已经运行。

## 0. 目标、边界与当前状态

在现有正式 Query 入口之外增加一个第二测试入口，使召回、有限补搜和回答生成均由 RAGFlow Agent Workflow 执行。现有 Chat 入口继续作为基线，不自动切换，也不因 Workflow 已存在而退休现有补丁。

第一版覆盖知识库、当前设备上下文、临时附件和可选联网；实时 EAM、同型号关系查询和自由 SQL/MCP 属于后续阶段。Ingestion Pipeline 使用独立样本库，覆盖 FILE_SHARE PDF、INLINE_JSON/JSONL，以及当前附件路径支持的 Office、图片、文本等格式。

当前不执行：

- 连接、重启、recreate 或部署 `192.168.30.30` 的任何服务；
- 中断 30 上正在运行的解析识别作业；
- 修改 30 的环境文件、数据卷或正在运行的镜像；
- 把测试 Workflow 设为正式默认入口。

当前可执行：

- 本地代码、DSL 模板、测试和文档准备；
- 本地静态检查、编译检查和不依赖外部服务的单元测试；
- 等待部署授权后，按 `docs/integration/update-30-server-agent.md` 执行最小服务更新。

## 1. 分层原则

```text
EAM
 │
 ▼
Gateway：Trust Boundary
 JWT / HMAC / Tenant / ACL / Asset Registry
 Authorized Scope G / 文件生命周期 / 外部 ID / Audit / Public Contract
 │
 ├───────────────┐
 ▼               ▼
Ingestion        Agent Workflow
 Pipeline         Query understanding / Retrieval / Tools / Evidence / Answer
 Parser/OCR      │
 Transformer     ▼
 Chunker       Gateway Response Adapter
 Indexer       Citation / Status / SSE / History / Memory write
 │
 ▼
Dataset / Retrieval Data Plane
```

判断规则：

```text
Gateway：用户和文档允许做什么
Workflow：本轮问题采用什么执行路径
Tool：执行一个受控能力
RAGFlow Core：底层解析、检索和运行时如何实现
```

认证、tenant、ACL、Asset Registry 权威身份、允许访问集合 G、文件所有权、版本生命周期、审计、引用文件下载授权、业务终态和公共协议不交给 LLM 或画布变量。

## 2. 当前 RAGFlow Core Patch 的迁移判断

| Patch | 当前作用 | Workflow 化建议 |
|---|---|---|
| RF-PATCH-001 | 部署入口和挂载兼容 | 保留部署层 |
| RF-PATCH-003 | 附件存在时跳过 `empty_response` 提前结束 | Workflow 显式处理附件后，可在新路径验证后退休；旧 Chat 路径仍需保留 |
| RF-PATCH-004 | Grounding、Prompt marker、reasoning、日志脱敏和可选 fuse | Grounding 编排可迁；日志安全、供应商兼容和底层保护继续保留 |
| RF-PATCH-005 | WebSearch 失败回退到已有内部知识 | 适合 Workflow 的分支；旧 Chat 路径未下线前不删除 |
| RF-PATCH-006 | 临时附件认证删除 | 保留 Gateway/Core |
| RF-PATCH-007 | `completed` / `no_reliable_evidence` / `failed` 显式终态 | 保留协议和适配；Workflow 重新接入终态 |
| RF-PATCH-008 | 安全过程可观察性 | 可复用指标，Canvas trace 仍需独立脱敏 |
| RF-PATCH-009 | 请求级 RAG 诊断 | 保留观测定义和旁路 |
| RF-PATCH-010 | 文档 DONE/FAIL/CANCEL webhook | Pipeline 可产生流程结果，基础设施通知和对账保留 |
| RF-PATCH-011 | Gateway 授权文档范围和 metadata restrict | 搜索策略可迁，授权硬边界必须在 Gateway/受控 Retrieval 中执行 |
| RF-PATCH-012 | Workflow Retrieval 硬 scope 与 Parser JSON 分支 | 新增最小上游补丁，需独立 ADR、测试和升级说明 |

迁移不能按“原生节点存在”推定“企业行为等价”。当前画布 Retrieval 需要显式接收并执行 `doc_scope_ids`/`doc_scope_mode=restrict`；Begin 中的提示词或普通变量不能作为权限控制。

## 3. Gateway 与 Workflow 的职责划分

### Gateway 保留

- JWT、HMAC、Console 身份和 tenant 校验；
- 用户/会话所有权、ACL 和当前可检索文档集合 G；
- Asset Registry canonical identity、文档版本、质量门和可检索状态；
- FILE_SHARE、INLINE_JSON、源路径、幂等、TTL、清理、回调和 outbox；
- 临时附件 ownership、MIME、大小、TTL、删除和 `indexPolicy=never`；
- 外部文档 ID、source version、page/bbox/asset 映射；
- Citation 文件访问授权、SSE、历史回放和业务终态；
- EAM 用户 Memory 的主体派生、查询隔离和成功后写回；
- Workflow agent ID、应用版本和运行会话绑定。

### Workflow 可以承接

- 问题理解、改写和执行策略分类；
- G 范围内的本轮搜索范围 S 和有限补搜；
- 知识库 Retrieval、证据充分性判断和回答综合；
- 联网许可开启时的可选 WebSearch；
- 当前附件作为本轮上下文的融合；
- 回答格式、引用候选选择和面向用户的说明。

Workflow 必须满足：

```text
S ⊆ G
```

模型不能自行构造 `doc_ids`、dataset、tenant、外部文档 ID、page/bbox、文件路径或下载 URL。

## 4. 第二测试入口、会话与版本

### 入口

Harness 增加“Agent Workflow 测试”导航项，复用现有聊天、附件、引用和运行日志界面。前端请求 Gateway 的：

```text
POST /enterprise/api/v1/workflow/conversations/{conversation_id}/messages
GET  /enterprise/api/v1/workflow/status
```

Gateway 再调用固定配置的 RAGFlow：

```text
POST /api/v1/agents/chat/completions
```

客户端不能传入任意 `agent_id`。服务端通过部署配置固定：

```text
ENTERPRISE_WORKFLOW_ENABLED=true
ENTERPRISE_WORKFLOW_AGENT_ID=<applied-agent-id>
ENTERPRISE_WORKFLOW_VERSION=enterprise-qa-agent-v1
```

未配置固定 ID 或版本时，入口返回 `WORKFLOW_NOT_CONFIGURED`，不回退到 Chat。

### 会话隔离

- Chat 与 Workflow 使用命名空间不同的幂等键；一个入口不会重放另一个入口的结果。
- `ext_v2_conversation` 保存 `workflow_agent_id`、`workflow_version`、`workflow_session_id`，与 Chat 的 `ragflow_session_id` 分开。
- 已有 Workflow 会话绑定版本不一致时拒绝继续；编辑草稿不影响已应用版本。
- 运行输入每轮重新计算，不能复用上一轮的授权范围。
- 应用版本后创建的新测试会话绑定该版本；旧会话继续使用旧版本，便于回滚和对照。

## 5. Enterprise QA Agent Workflow v1

### 主流程

```text
Gateway
  ├─ authenticate / ownership / ACL
  ├─ calculate authorized scope G
  ├─ read current-user Memory
  └─ secure attachment observation/upload
        ↓
Begin
  ├─ query / history / business context
  ├─ authorized_dataset_ids
  ├─ authorized_doc_ids
  ├─ attachment context
  └─ user_memory
        ↓
Agent
  ├─ Knowledge Retrieval (restricted)
  ├─ optional bounded re-query
  ├─ optional WebSearch when explicitly allowed
  ├─ evidence sufficiency
  └─ final answer with [ID:n] markers
        ↓
Message
        ↓
Gateway
  ├─ citation authorization and external mapping
  ├─ explicit terminal status
  ├─ JSON/SSE/history persistence
  └─ async user-memory candidate after successful persistence
```

首版画布模板位于 `enterprise/workflows/enterprise_qa_agent_v1.template.json`，文件采用 RAGFlow Agent 导入器要求的 canonical DSL：`graph.nodes`、`graph.edges` 和 `components` 位于顶层；不能再把它包进额外的 `dsl` 对象。模板包含：

- Begin 的授权 dataset/document 输入、业务上下文、附件和 Memory 输入；
- Agent 的 Retrieval 工具，使用 `begin@authorized_dataset_ids` 和 `begin@authorized_doc_ids`；
- v1 默认不配置 Web Search；联网版本需单独评审，真实 provider key 不进入 Git；
- 强制引用格式、无可靠证据和禁止生成内部安全字段的提示；
- Message 流式输出。

模型和工具预算初始沿用现有五档：`simple/low/medium/high/ultra` 的检索上限为 `1/1/2/3/4`，联网每次运行最多一次。联网默认关闭；首版不开放 SQL、任意 HTTP/MCP、KG、TOC 和导航工具。

### 证据与终态

Workflow 产出的是证据候选和回答；Gateway 负责把引用映射为外部安全投影。空证据、工具错误和模型错误分别处理：

```text
completed             有可靠证据且完成回答
no_reliable_evidence  运行成功但证据不足
failed                工具、模型、协议或运行失败
```

`citations` 与业务状态相互独立，不能用其中一个推导另一个。没有可靠业务证据时，用户 Memory 也不能把状态改成 `completed`。

保留 `run.started`、`answer.delta`、`answer.replaced`、`citation`、`answer.completed`、
`run.failed` 和取消语义；兼容解析 `reasoning.delta`，但不转发 Canvas 的原始思维过程。
Canvas 的节点 trace、Prompt、原始 chunks 和附件正文不进入公共诊断。

### 原生 Retrieval 的硬约束

`RF-PATCH-012` 为 `RetrievalParam` 增加：

```text
doc_scope_ids
doc_scope_mode
```

`doc_scope_ids` 可以是 Gateway 注入的画布引用，例如 `begin@authorized_doc_ids`；`restrict` 模式下：

- scope 缺失或为空直接返回空结果；
- metadata filter 只能在该集合内进一步缩小；
- 父子块、TOC 和其他扩展读取不得扩大集合；
- `use_kg` 等没有文档范围参数的全库扩展在 `restrict` 模式下关闭；
- 不能省略过滤参数后查询全库。

## 6. EAM 用户 Memory

当前未提交代码中的 Memory 实现继续作为唯一 Gateway Memory 入口：

- `memory_subject.py` 从认证 `UserPrincipal` 生成 `eam:{tenant_id}:{business_user_id}`；
- `memory_client.py` 调用 RAGFlow Memory 搜索和写入接口；
- `user_memory.py` 在问答前读取、成功后异步提交候选；
- 读取失败/超时返回空上下文，写入失败不阻塞问答。

### 读取

每次 Workflow run 由 Gateway 调用 `fetch_user_memory_text()`，再把结果以 `user_memory` 输入传给 Begin/Agent。Memory 只用于：

- 用户表达偏好；
- 历史指代辅助；
- 解释用户问题的背景。

Memory 不得：

- 扩大 G 或修改 ACL；
- 覆盖当前 EAM/文档事实；
- 成为引用来源；
- 让无证据回答变成成功回答；
- 进入 Ingestion Pipeline 的文档 Metadata 或共享索引。

当前权威业务上下文优先于历史记忆。记忆和当前事实冲突时，以当前授权证据和当前 EAM 上下文为准。

### 写回

在 Gateway 完成最终引用处理、终态判定和消息持久化后，仅对 `completed` 且最终问题/答案非空的运行调用 `schedule_memory_candidate()`。

- 写入问题和最终公开答案；
- 不写中间答案、思维过程、原始 chunks、附件正文或未替换的流片段；
- Workflow 使用独立 `chat_id` 来源标识，用户主体仍是认证主体；
- 失败、取消、无可靠证据和幂等回放不写入；
- 写入失败不改变已完成的问答结果；
- 不启用 Canvas 自带的另一套 Memory 自动写回，避免重复候选。

### 隔离验收

覆盖同用户跨会话、同租户不同用户、不同租户相同用户 ID、伪造用户字段、Workflow 版本变化、Memory 服务失败、记忆与当前事实冲突、成功写回和失败不写回。日志只记录启用状态、命中数、耗时、错误类型和调度状态，不记录记忆正文。

## 7. 多格式 Ingestion Pipeline v1

### 输入与责任

当前正式外部输入继续是 Document Feed 3.2 的 FILE_SHARE PDF 和 INLINE_JSON。Gateway 负责来源、HMAC/服务身份、tenant、幂等、版本、权威 Metadata、质量门、状态回调、原文件所有权和访问授权。Pipeline 负责解析、视觉增强、结构化切块和索引输入。

### 文件分支

| 类型 | 首版策略 |
|---|---|
| 原生纯文本 PDF | DeepDoc，保留结构/页码/位置；作为轻量解析的对照样本 |
| 扫描 PDF | DeepDoc OCR、版面、表格和位置解析 |
| 原生图文 PDF | DeepDoc 提取文字和图片区域，按配置用视觉模型补充图片语义 |
| 混合 PDF | 同一流程处理文字页、扫描页、表格和图片，不按文件标签丢弃内容 |
| INLINE_JSON / JSONL | 复用 `JsonParser`，保留对象/数组记录边界，不当普通长文本切断 |
| Word/PPT | 原生 JSON 结构输出，保留标题、段落和幻灯片层级 |
| Excel/CSV | 原生 HTML/结构化输出，保留表头、行列和单位 |
| JPG/PNG | OCR；可选视觉理解，保留图片证据 |
| TXT/Markdown/HTML/代码 | 原生文本/JSON 解析，按自然段和结构切块 |

纯文本 PDF、扫描 PDF、图文 PDF 和混合 PDF 作为独立质量维度验收。首版不使用当前缺少完整位置输出的 `plain_text` 分支作为生产默认。

### Pipeline

```text
Gateway source validation / idempotency / version / trusted metadata
        ↓
File
        ↓
Parser
  ├─ PDF DeepDoc
  ├─ JSON/JSONL JsonParser
  ├─ Office / spreadsheet / image / text parsers
  └─ preserve structure, page, position and media
        ↓
TokenChunker
  ├─ paragraph/step boundaries
  ├─ table/image units kept intact
  └─ Token Chunker before optional Title Chunker
        ↓
Tokenizer / index input
        ↓
Gateway quality evaluation and publication gate
        ↓
retrievable / review_required / failed
```

首版模板位于 `enterprise/workflows/eam_ingestion_pipeline_v1.json`，使用 Parser、TokenChunker、Tokenizer；PDF 使用 DeepDoc，JSON/JSONL 使用新增 JSON 分支。章节型说明书可在 30 上单独复制模板后加入 TitleChunker；不要强制所有 JSON、表格和图片走标题切块。

### Metadata、Transformer 与质量

- Gateway 注入的 tenant、设备身份、externalDocumentId、sourceVersionId、ACL 和 business status 不能被 Parser/LLM 覆盖；
- Transformer 只追加检索辅助字段，如摘要、关键词、文档类型和故障码提示；不默认开启问题生成；
- 图片视觉失败进入质量结果，不能伪装成已完成语义增强；
- Pipeline DONE 不等于生产可检索；Gateway 继续检查 text coverage、乱码率、table/position、关键字段、citation 质量和版本发布门；
- 质量不通过的文档不进入正式 G；重试不得产生重复业务版本；
- 身份 Metadata 更新优先走现有更新 API，避免无必要地重新解析；
- Ingestion 不读取或写入个人 Memory，不把文档正文批量写入 Memory。

### JSON 约束

`RF-PATCH-012` 的 Parser JSON 分支调用现有 `JsonParser`，输出 `{"text": ..., "doc_type_kwd": "text"}` 的结构化记录。嵌套对象、数组、JSONL 每条记录和 0/false 值必须保留；不要求非分页格式虚构 page/bbox。

## 8. 本地实现与配置文件

已加入：

- `enterprise/gateway/query/workflow_client.py`：RAGFlow Agent API transport 和离线 stub；
- `enterprise/gateway/query/workflow_router.py`：第二入口、Scope、附件、引用、Memory、终态和 Workflow session 适配；
- `enterprise/gateway/db/tables.py` / `schema.py` / `query/v2_store.py`：Workflow session/version 独立绑定；
- `ragflow/agent/tools/retrieval.py`：受控 `doc_scope_ids`/`restrict`；
- `ragflow/rag/flow/parser/parser.py`：JSON/JSONL Parser 分支；
- `enterprise/web` Harness：`workflow` 模式和第二导航入口；
- `enterprise/workflows/enterprise_qa_agent_v1.template.json`；
- `enterprise/workflows/eam_ingestion_pipeline_v1.json`；
- `enterprise/workflows/README.md`。

模板中的 `__REPLACE_WITH_CHAT_LLM_ID__`、`__CONFIGURE_OPTIONAL_PROVIDER__` 仅是占位符，真实模型和 provider 配置必须在部署环境的受控配置中完成，不写入 Git。

## 9. 验证和 30 部署门禁

### 验证矩阵

- ACL：跨租户、范围外文档、空 G、metadata 零命中、父子块和嵌套 Retrieval；
- 问答：单事实、多文档比较、有限补搜、附件单独问答、附件+知识库、无证据、联网关闭和联网失败；
- 协议：JSON/SSE/历史、幂等重放、取消、流中失败、最终替换和引用投影；
- Memory：不同用户/租户隔离、读取降级、成功写回、失败不写回、与事实冲突；
- Pipeline：纯文本/扫描/图文/混合 PDF、表格、嵌套 JSON、JSONL、Office、图片和文本；
- 安全：日志、trace、DSL 快照和 artifact 不含凭据、Prompt、知识正文、Memory 正文或模型思维过程。

### 当前暂停

30 上正在执行解析识别作业，本阶段不执行任何部署、重启、镜像导入、Pipeline 导入、样本上传或真实问答。恢复后必须先重新检查作业状态和端口，再按 `docs/integration/update-30-server-agent.md`：

1. 只构建和更新本次涉及的服务；
2. 使用 `--no-deps --pull never`，不重建 MySQL/ES/MinIO/Redis；
3. 保留旧镜像/tag 和数据卷，记录 source commit、镜像 digest、Compose 与补丁清单；
4. recreate 后检查 30 上端口仍为 `0.0.0.0`；
5. 从开发机访问 8080、9380、5188、3000，不能只 curl 30 本机 `127.0.0.1`；
6. 只在健康、marker、最小 HTTP 和引用/终态检查通过后开始样本调优。

### 调优方式

先用同一批现有文档对比 Chat 和 Workflow，固定用户身份、Memory 夹具、模型和检索参数；每轮只改一类参数，记录 Workflow 版本、首段正文时间、总耗时、模型调用量、召回数量、回答评审和引用评审。再用相同原始样本对比旧解析与 Pipeline，避免把解析变化和查询变化混为一谈。

正式切换的最低条件是：授权负例不回退、空范围不查全库、引用可回映、终态正确、SSE/历史一致、取消和失败可收尾、Memory 隔离通过、典型设备问题质量不低于 Chat、性能和成本可接受，并且确实减少了需要维护的编排代码。

## 10. Ingestion Pipeline 详细输入契约

Gateway 在调用 Pipeline 前生成 Trusted Ingestion Context。下面是字段语义，实际
请求仍通过 Document Feed 3.2 的 FILE_SHARE/INLINE_JSON 契约传递：

```json
{
  "tenantId": "wp04e2e",
  "externalDocumentId": "EQ-CF-001-MANUAL",
  "sourceVersionId": "v20260914-001",
  "sourceSystem": "EAM",
  "sourceKind": "FILE_SHARE",
  "equipmentId": "EQ-CF-001",
  "fixedAssetNo": "FA-10001",
  "documentType": "operation_manual",
  "documentTitle": "离心机操作维护说明书",
  "filePath": "<gateway-resolved-readonly-file>",
  "sha256": "<fixture-hash>",
  "metadata": {
    "equipment_type": "离心机",
    "model": "ABC-200",
    "manufacturer": "<fixture>",
    "department": "<fixture>",
    "document_type": "operation_manual"
  },
  "parsingProfile": "equipment-scan-v1"
}
```

`tenantId`、设备身份、externalDocumentId、版本、ACL 相关字段均是 Gateway
trusted data。Parser 或 LLM 只能增加检索辅助 Metadata，不能根据正文覆盖这些字段。

概念上的处理链保持为：

```text
Parser/OCR/Layout/Table
        ↓
Document Metadata Transformer
        ↓
Token/Title/Hierarchical Chunker
        ↓
Keyword/Summary Transformer（按 profile 选择）
        ↓
Indexer：全文 + 向量 + Metadata + 位置
```

当前可执行模板使用 RAGFlow v0.26.4 画布中已经可用的
`Parser → TokenChunker → Tokenizer`；Transformer 和质量门先由固定 profile 与
Gateway 旁路承担，待 30 上验证 DSL 节点输出后再逐个加入。

### Parser Profile

Parser 由服务端按文件类型和质量策略选择，EAM 不能直接指定任意解析器：

```yaml
profile: equipment-scan-v1
pdf:
  parser: deepdoc
  output: json
json:
  parser: JsonParser
  suffix: [json, jsonl, ldjson]
chunk:
  token_size: 640
  overlap: 0.12
  title_hierarchy: optional
keywords:
  enabled: true
question_generation:
  enabled: false
index:
  hybrid: true
```

扫描 PDF、纯文本 PDF、图文 PDF 和混合 PDF 都先走 DeepDoc profile；以同一批样本
分别统计 text coverage、乱码率、表格完整性、位置覆盖率、图片语义成功率和引用
页码准确率。纯文本 PDF 可以作为轻量解析对照组，但没有完成位置校验前不把
`plain_text` 作为生产默认。

JSON/JSONL 使用上游已有 `JsonParser`：嵌套对象、数组、每条 JSONL 记录以及
`0`/`false` 值必须保留；非分页格式不虚构 page/bbox。Office、表格、图片、文本、
HTML、邮件和 EPUB 按 ParserParam 的对应分支处理，音视频只有在明确配置 OCR/VLM
后才进入质量评测。

### Metadata、Chunk 和 Index

Document Metadata Transformer 只产生 `model`、`manufacturer`、`document_date`、
`certificate_no`、`fault_codes`、`manual_section_type` 等 Retrieval Hint；不生成
tenant、ACL、equipmentId、fixedAssetNo、externalDocumentId 或 sourceVersionId。

初始文本 chunk 为 512--800 tokens、10%--15% overlap；章节型说明书可以在独立
profile 中启用 Title/Hierarchical Chunker。表格、维修记录、检测结果、证书和图纸
描述尽量保持结构单元完整，避免跨 chunk 拆断表头、单位和数值。

Indexer 至少保留：

```text
ragflowDocumentId / externalDocumentId / sourceVersionId
equipmentId / fixedAssetNo / documentType
pageNo / bbox / parentChunkId
parserProfile / parserVersion / embeddingVersion
```

Citation 必须能从 `External Document → Version → Page → Region` 回到原始
FILE_SHARE。Pipeline 的完成状态不等于生产可检索，Gateway Quality Gate 仍决定
`retrievable`、`review_required` 或 `failed`。

### FILE_SHARE 与 INLINE_JSON 生命周期

```text
EAM → Gateway HMAC/tenant/path/version/idempotency
    → Asset Registry canonical identity
    → Trusted Metadata + fixed profile
    → RAGFlow Pipeline
    → DONE → Gateway Quality Gate
    → current version / review / failed → callback/outbox
```

客户文件服务器仍是原件权威来源，RAGFlow 不改变文件所有权。版本切换、停用、
恢复、删除、幂等和回调继续由 Gateway 控制。`INLINE_JSON` 走 JSON Parser 和独立
Normalize 分支，可与 PDF 进入同一 Dataset，也可在评测后拆分 Dataset；它不应被
强制送入 DeepDoc。

第一阶段不把 Quality Gate、Idempotency、Version Lifecycle、FILE_SHARE、Source
Validation、Callback、Asset Registry、ACL、Tenant、External Citation 和 Audit
下沉到 Pipeline。

## 11. Agent Workflow 详细执行路径

### Begin 输入契约

Gateway 只传服务端生成的值，实际 RAGFlow API `inputs` 使用 Begin 的 typed value
形式：

```json
{
  "authorized_dataset_ids": {"type": "array", "value": ["dataset-1"]},
  "authorized_doc_ids": {"type": "array", "value": ["doc-1", "doc-2"]},
  "doc_scope_mode": {"type": "line", "value": "restrict"},
  "business_context": {"type": "object", "value": {"model": "ABC-200"}},
  "user_memory": {"type": "line", "value": ""},
  "internet_enabled": {"type": "boolean", "value": false},
  "reasoning_mode": {"type": "line", "value": "simple"},
  "workflow_version": {"type": "line", "value": "enterprise-qa-agent-v1"}
}
```

前端不能传 tenant、role、dataset 或 document scope；Workflow 不能修改这些输入。

### 问题分类

分类只选择执行策略，不作权限判断：

| 类别 | 例子 | 初始执行路径 |
|---|---|---|
| `DIRECT_QA` | EQ-001 的额定功率是多少？ | 一次 Restricted Retrieval → Answer |
| `RESEARCH` | 为什么经常振动报警？ | 改写问题 → 最多 4 轮检索 → Evidence Check |
| `BUSINESS_FUSION` | 比较 EQ-001 与同型号设备故障 | 后续接入受控 EAM/Asset Tool，再与 KB 合并 |
| `ATTACHMENT_QA` | 结合上传的故障照片和说明书 | Attachment Context + Restricted Retrieval |

第一版 Agent 模板先实现知识库、附件和证据回答；EAM 实时事实、同型号关系、SQL
和 MCP 在通过工具授权评审后再加。

### 受控动态范围

`G` 是 Gateway 计算的最大授权集合，`S` 是 Agent 为本轮选择的搜索集合，始终满足
`S ⊆ G`：

```text
用户问 “比较 A 和 B”
requested = {A, B}
S = requested ∩ G

用户问 “和同型号其它设备比较”
Asset Relation Tool → candidates
S = candidates ∩ G
```

会话上下文可以保存 A、B 的业务指代，但每轮的文档集合都重新从当前 ACL 和质量
投影计算。LLM 不能构造 `doc_ids` 后直接查询，也不能用一次检索结果给下一轮
扩大授权范围。

### 工具和分支

- **Knowledge Retrieval**：Hybrid Search、metadata filter、rerank 和 chunk reference；
  `doc_scope_mode=restrict` 是硬边界。
- **Asset Relation Tool**：只返回 Gateway 已授权的设备关系候选，Agent 再求交集。
- **EAM Facts Tool**：只读设备台账、维修、保养、故障和当前状态，禁止自由 SQL。
- **Attachment Context Tool**：只读取当前 conversation 已授权附件；不建持久索引，
  不跨会话复用。
- **Web Search**：`internetAllowed=false` 默认关闭；打开后最多一次，失败继续使用
  内部证据，不把网页结果伪装成内部文档引用。

回答前经过 Evidence Gate：知识 chunk、受控 EAM record 或附件观察至少有一种可靠
依据；否则返回 `no_reliable_evidence` 和固定的“未找到可靠依据，无法回答。”。用户
Memory 只能辅助指代和表达偏好，不能充当 EvidenceSet。

### 引用、状态和观察

Agent 只返回自己采用的 `evidence_id` 或 `[ID:n]` 候选；Gateway 再检查授权并映射：

```text
evidence_id → authorization check → externalDocumentId/version/page/bbox/sourceType
```

Agent 不生成外部 ID、页码、bbox、文件路径或下载 URL。公共终态固定为
`completed`、`no_reliable_evidence`、`failed`，并与 citations 独立持久化。SSE 保留
`run.started`、`answer.delta`、`answer.replaced`、`citation`、`answer.completed`、
`run.failed` 和取消语义；兼容解析 `reasoning.delta`，但不转发 Agent 的原始思维过程。

每个 Workflow run 记录 `runId`、Workflow version、类别、工具名、工具耗时、检索次数、
chunk 数、Evidence 数、LLM 耗时、首段时间、总延迟和终态；不记录完整 Prompt、chunk
正文、附件正文、JWT、工具 secret、原始模型输出或模型思维过程。

## 12. Gateway 精简目标与迁移顺序

稳定目标是让 Gateway 只保留：

```text
Auth / Tenant / ACL / Asset Identity / Authorized Scope
Public Contract / Conversation Ownership
File & Attachment Security / External Citation / Audit / Agent Proxy
```

Workflow 承担：

```text
Intent / Rewrite / Search Strategy / Dynamic S
Multi-step Retrieval / EAM Tool / Attachment Fusion
Web Fallback / Evidence Sufficiency / Answer Strategy
```

`reasoningMode` 在过渡期继续作为成本和工具预算提示，并保留现有
`simple/low/medium/high/ultra` 的入口兼容；统一 Workflow 后由 Agent 根据问题复杂度
决定是否补搜，不能把它当作 ACL 或授权开关。

迁移分三步：

1. 保留全部安全和协议补丁，新增 Workflow 作为可选基线，使用同一组评测样本与
   现有 Chat 做 A/B。
2. 先迁 WebSearch fallback、reasoningMode 对应的检索策略、A/A∪B/同型号范围、
   KB+EAM+附件融合；每次只退休一块编排逻辑。
3. 在答案质量、ACL、引用、终态、SSE/历史、Memory、延迟和成本均达标后，再评估
   RF-PATCH-003、RF-PATCH-005、RF-PATCH-004 的编排部分及 RF-PATCH-011 的查询传播
   部分。ACL、Authorized Scope、Quality Gate、Citation Security、附件生命周期和
   低层诊断继续保留。

## 13. 第二入口验收样例

```text
Case 1  “EQ-001 厂家是什么？”
        DIRECT_QA；一次 Restricted Retrieval；不调用 EAM Tool。

Case 2  “EQ-001 为什么经常振动报警？”
        RESEARCH；必要时多轮检索；证据不足则 no_reliable_evidence。

Case 3  “和其它同型号设备比较。”
        Asset Relation candidates → candidates ∩ G → Retrieval/EAM。

Case 4  用户无 B 权限但要求比较 A/B。
        requested={A,B}、G={A,C}、S={A}；B 不进入 Retrieval。

Case 5  无可靠证据。
        返回固定无依据答案；不使用 Memory 或模型常识补全设备事实。
```

调优时固定用户身份、Workflow version、模型、Memory fixture 和样本集，同时保留
旧 Chat 的结果作为 baseline。每轮只调整一种参数，记录首段时间、总耗时、调用量、
召回数量、回答评审、引用评审和失败类型。解析评测与查询评测分开进行，避免把 PDF
解析变化误判成 Agent 策略收益。
