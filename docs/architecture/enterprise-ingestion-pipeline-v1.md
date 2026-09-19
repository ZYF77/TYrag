# Enterprise Ingestion Pipeline v1

> Companion architecture note for the multi-format Ingestion Pipeline.
> Full combined design: `docs/architecture/tyrag-agent-workflow-and-ingestion-v1.md`.
> ADR: `decisions/ADR-021-Agent-Workflow管控范围与多格式Pipeline.md`.
> Patch: `RF-PATCH-012` (Parser JSON/JSONL branch).

## Scope

- Data Flow canvas template: `enterprise/workflows/eam_ingestion_pipeline_v1.json`.
- Parser first-class JSON/JSONL/LDJSON via existing `JsonParser`, preserving record boundaries.
- PDF layout/OCR/tables/images through DeepDoc; TokenChunker → Tokenizer.
- Gateway still owns source validation, idempotency, authoritative metadata, quality admission, version lifecycle, callbacks.

## Hard constraints

1. Ordinary Parser defaults for non-JSON formats remain unchanged.
2. JSON branch must not flatten nested objects into undifferentiated plain text.
3. Do not use incomplete `plain_text` PDF branch as production default in v1.
4. Pipeline import / sample upload on server 30 is gated until parse jobs finish.

## Local artifacts

- `ragflow/rag/flow/parser/parser.py` (`setups["json"]`, `_json`)
- `enterprise/workflows/eam_ingestion_pipeline_v1.json`
- Unit/static: `ragflow/test/unit_test/rag/flow/test_parser_json_setup.py`, `enterprise/scripts/validate_workflow_artifacts.py`

## Deploy / E2E (out of this finish)

RF image rebuild required for Parser branch. Formal PDF/JSON/Office quality matrix runs after 30 deploy authorization.

## Extracted detail from combined doc

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
