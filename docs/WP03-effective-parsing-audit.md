# WP-03 Effective Parsing Audit

审计对象是当前工作区的 Enterprise ingestion 适配层和随附的 RAGFlow v0.26.4
源码。本文只记录实际源码调用链、可验证能力和验收计划；不把设计文档、合成
PDF 或 stub 的结果当成生产证明。

## 范围、冻结项和证据规则

- 本收尾只允许修改 `enterprise/` 的 WP-03 同步/质量/评测代码、WP-03 测试和本审计文档。
- 冻结：v2 冻结文档、v2 OpenAPI、v2 router/store、鉴权实现、RAGFlow 上游源码、根锁文件。
- Enterprise 状态仓储当前是 `aiosqlite`；配置中的 `PG_*` 没有对应的 repository 或调用链，因此 PostgreSQL 集成在本 WP-03 运行中标记 `not_applicable`，不是假绿色。
- `PROVEN` 仅允许真实脱敏 S1–S8 文件、真实 RAGFlow/对象存储、正负查询、ACL 负向和重复解析证据同时存在时使用。缺少样本或 live 环境时，正式 WP03 必须是 `BLOCKED`（runner exit 2），不能用 `skip`、`xfail` 或合成报告替代。

## 1. 总结

当前实现已经形成一条真实可执行的链：Enterprise 收件 → outbox → 对象存储读取 → RAGFlow Dataset/Document → 文档级 parser 配置回读 → 解析 → DeepDOC/图片/表格 parser → chunk → embedding → doc store/Elasticsearch → Enterprise quality evaluation。WP-03 的 parser application 现在保存 `selected/configured/executed` 和 readback 结果，质量通过前不会提升为 current，旧版本保持可用。

但是，真实 S1–S8 样本清单和其 SHA-256 证明尚未提供；因此本次结论是：

> 代码路径为 `IMPLEMENTED_NOT_PROVEN`；正式 WP03 验收保持 `BLOCKED`。任何“生产已支持扫描 PDF/流程图”的表述都不能写成 `PROVEN`。

已能由单元/契约测试证明的仅是确定性路由、PATCH→GET→parse 顺序、readback mismatch 阻断、quality fail-closed、版本切换和 runner 退出码语义；这些测试不等价于真实文档效果。

## 2. Actual Ingestion Call Graph

```text
Enterprise POST /documents
  app.py:425-523 upsert_document
      └─ sync/worker.py:31-52 OutboxWorker.run_once
          └─ sync_service.py:166-251 process_event
              └─ sync_service.py:253-333 _sync_event
                  ├─ SourceAdapter.fetch (对象存储，校验 sha256)
                  ├─ sync_service.py:293-318 ensure Dataset + register
                  └─ sync_service.py:355-433 _register_ragflow
                      ├─ public upload/list
                      ├─ sync_service.py:435-525 _ensure_parser_configured
                      │   ├─ routing.py:30-117 route_document
                      │   ├─ RAGFlow PATCH document_api.py:191-321
                      │   └─ GET readback; mismatch/legacy_unverified => terminal fail
                      ├─ public POST parse
                      │   ragflow_document_client.py:173-187
                      │   document_api.py:1536-1649
                      └─ terminal GET + _record_terminal_parser_evidence:527-562
                          └─ ready + quality job:587-621
                              └─ RAGFlow DocumentService.run:1221-1241
                                  └─ task_service.queue_tasks:439-550
                                      └─ chunk_builder.py:39-112
                                          ├─ parser factory: naive/table/picture
                                          └─ parser.chunk(... parser_config ...)
                                              └─ task_handler.py:568-695
                                                  ├─ ChunkService.build_chunks:91-177
                                                  ├─ EmbeddingService.embed_chunks:617-632
                                                  └─ ChunkService.insert_chunks:239-385
                                                      └─ docStoreConn.insert (ES/Infinity)
```

RAGFlow 的公开 chunk GET/list 回到 `chunk_api.py:461-548`，返回
`document_id/content/image_id/doc_type_kwd/positions`；Enterprise formal query
再把这些字段映射为外部 document/version、page、bbox、positions 和 evidence。

## 3. `recorded_only` 根因与当前状态

基线问题是 routing 只产生了审计字段，没有保证 RAGFlow 的 document 在 parse
前真的采用该 profile。结果会出现“数据库写了 DeepDOC、RAGFlow 仍按旧配置解析”。

当前状态如下：

| 状态 | 保存/读取位置 | 进入 RAGFlow 的动作 | 结论 |
|---|---|---|---|
| `selected` | `ExtDocumentMap.parser_profile/parser_profile_version`（`enterprise/gateway/sync/models.py:40-45,180-185`） | `SyncService._ensure_parser_configured`（`435-461`）选出 profile | 已实现，仍不是执行证明 |
| `configured` | `parser_configured_json`（`sync/models.py:680-717`） | 文档 `UNSTART` 时 PATCH `chunk_method/parser_config/meta_fields`，随后 GET（`sync_service.py:475-525`） | 公开 API 回读后才成立 |
| `executed` | `parser_executed_json` + `parser_application_status` | terminal GET（`sync_service.py:527-562`）比较实际 `parser_config` | mismatch 或 legacy 不得通过 |
| `E2E_PROVEN` | 仅在正式 S1–S8 报告/manifest/重复运行中 | 需要真实文件、chunk、embedding、citation、ACL | 当前缺失，故 BLOCKED |

profile 是**文档级**的：公开 PATCH 路径为
`/datasets/{dataset_id}/documents/{document_id}`（`document_api.py:191`），其中
`chunk_method=naive` 选择 `rag.app.naive`，`parser_config.layout_recognize=DeepDOC`
再选择 DeepDOC layout；不是一个新的 parser id。Dataset 只负责归属和索引，当前
WP-03 没有把 Dataset 全局配置当作执行证据。PDF（数字、扫描、混合、表格、图示）
统一走 `pdf_deepdoc_v1`；图片走 `picture`；CSV/XLS/XLSX 走 `table`。

已上传但在 parse 前没有回读的旧文档会写成 `legacy_unverified` 并被质量门阻断；
这正是从 `RECORDED_ONLY` 迁移到可审计状态的兼容策略。

## 4. Effective Capability Matrix

状态只使用 `PROVEN / IMPLEMENTED_NOT_PROVEN / RECORDED_ONLY / MISSING / BLOCKED`。

| 能力 | 文档要求 | RAGFlow 存在 | 当前调用/证据 | E2E 证明 | 状态 |
|---|---|---|---|---|---|
| 数字 PDF | 正文、页码、chunk | `naive` + DeepDOC | `naive.py:1041-1092` | 无真实 S1–S8 | IMPLEMENTED_NOT_PROVEN |
| 扫描 PDF OCR | raster → OCR → text | `Pdf.__images__`/`__ocr` | `pdf_parser.py:1608-1742,771-875` | 无 S1 | IMPLEMENTED_NOT_PROVEN |
| 混合 PDF | 原生文本与扫描页并存 | 有两条文本/OCR路径 | `pdf_parser.py:1625-1655` | 无 S6 | IMPLEMENTED_NOT_PROVEN |
| 旋转/低质量页 | 纠偏、可控降级 | 表格自动旋转，页面空结果重试高分辨率 | `pdf_parser.py:470-552,1739-1742` | 无 S7a/S7b | BLOCKED |
| Table detection | 找到表格区域 | layout + TSR | `pdf_parser.py:1298-1318,470-593` | 无 S2 | IMPLEMENTED_NOT_PROVEN |
| Table structure | 行列/跨页结构 | `TableStructureRecognizer` | `pdf_parser.py:1472-1483` | 无 S2 cell accuracy | IMPLEMENTED_NOT_PROVEN |
| Table Markdown | Markdown 作为稳定结果 | 当前默认构造 HTML/rows | `pdf_parser.py:1482` + `nlp/__init__.py:473-505` | 无 | MISSING |
| Table crop | 原图区域可回看 | crop 后随 chunk 上传 | `pdf_parser.py:1386-1445`; `chunk_service.py:179-228` | 无 S2 crop | IMPLEMENTED_NOT_PROVEN |
| Image crop | figure/image crop | DeepDOC/picture 均可产生 image | `pdf_parser.py:1311-1469`; `picture.py:40-100` | 无 S3/S4 | IMPLEMENTED_NOT_PROVEN |
| Image OCR | 图片中文字进入文本 | PaddleOCR→本地 OCR fallback | `picture.py:65-82,113-151` | 无 | IMPLEMENTED_NOT_PROVEN |
| Image caption | caption/description 进入 embedding | optional VLM | `figure_parser.py:102-141,251-292` | 无 | IMPLEMENTED_NOT_PROVEN |
| Flowchart detection | 流程图节点/箭头/分支 | 只有 generic `figure` layout | `pdf_parser.py:1311-1318` | 无 S5 | MISSING |
| Flowchart VLM description | 理解 yes/no/branch/warning | generic figure prompt | `figure_parser.py:254-288` | 无 | IMPLEMENTED_NOT_PROVEN |
| page/bbox | page + `[left,right,top,bottom]` | parser positions | `pdf_parser.py:1423-1427,1477-1483`; `nlp/__init__.py:931-943` | 无真实 bbox IoU | IMPLEMENTED_NOT_PROVEN |
| chunk | 可检索文本/媒体 chunk | `ChunkService` | `chunk_service.py:91-177` | 无 | IMPLEMENTED_NOT_PROVEN |
| embedding | chunk 向量 | `EmbeddingService.embed_chunks` | `task_handler.py:617-632` | 无 | IMPLEMENTED_NOT_PROVEN |
| retrieval | chunk + position/image_id | RAGFlow search/doc store | `nlp/search.py:690-717`; `chunk_api.py:523-548` | 无 recall | IMPLEMENTED_NOT_PROVEN |
| citation | 回到 document/version/page/bbox | Enterprise formal mapping + public chunk GET | `formal_router.py:250-290,1049-1125` | 无真实正负查询 | IMPLEMENTED_NOT_PROVEN |
| quality gate | parser/声明/指标/版本硬门 | worker + gate + SQLite promotion | `quality/worker.py:477-534`; `quality/gate.py:128-172`; `sync/models.py:721-766` | 无真实样本 | IMPLEMENTED_NOT_PROVEN |

## 5. Scan PDF Findings

DeepDOC 对每页以 `72 * zoomin`（默认 `zoomin=3`）光栅化，先取 pdfplumber 字符；
乱码检测会清空该页字符，随后 OCR detect/recognize。OCR 结果进入 layout recognizer、
table transformer、text merge，再抽取 table/figure 和正文 sections。没有可用 boxes 时
会在 `zoomin < 9` 下递归提高分辨率（`pdf_parser.py:1739-1742`）。表格另有自动旋转、
旋转后重 OCR 和坐标映射（`470-552,638-769`）。

风险是页面级部分失败不会自动变成 Enterprise 失败：OCR detect 为空会追加空 box
（`771-779`），`__images__` 捕获异常后继续返回 parser（`1659-1661`），最终可能是
零 chunk。Enterprise 目前能看到 RAGFlow terminal run、chunk 数、page coverage 和
garbled ratio，但没有逐页 OCR error/rotation/fallback 清单。S1/S6/S7 必须把这些
字段加入报告，否则只能 `IMPLEMENTED_NOT_PROVEN`，不能以 `run=DONE` 代替 OCR 成功。

## 6. Table Findings

1. Layout 将 `table` 区域交给 TSR；跨相邻页的表格可以合并。
2. crop 产生带页坐标的图片；`construct_table(..., html=True)` 默认返回 HTML/rows。
3. `tokenize_table` 将字符串原样作为 `content_with_weight`，并写入 `doc_type_kwd=table`；
   有图片但没有 `<tr>` 时会降为 `image`。当前没有 Markdown 输出保证，因此“表格优先
   Markdown”不满足，需要后续 approved adapter 或明确把 HTML 定为契约。
4. RAGFlow chunk REST 已返回 `image_id` 和全部 `positions`（`chunk_api.py:505-518,532-545`），
   Gateway 可以取得它们；但没有表格 cell 级 citation 语义，只有区域 bbox。
5. crop 是否真正可读、表格结构是否正确，仍需 S2 的 table cell accuracy、crop hash
   和正向 citation 证明；不能从 parser 源码存在推导质量。

## 7. Figure / Flowchart Findings

DeepDOC 将 generic `figure` 区域 crop，并把 caption/text 与图片一起返回；若 figure 没有
任何文字，`_extract_table_figure` 的 `if not txt: continue` 会跳过该 figure
（`pdf_parser.py:1451-1455`）。因此“页面上有图”不等于“图进入 retrieval”。成功的
figure chunk 通过 `image2id` 写入对象存储，索引只保存 `img_id`。

流程图没有专用节点、边、分支或 warning 解析器；当前只得到一个 generic figure crop，
可能带 OCR/caption。不得把普通 OCR 或 generic figure 当成流程图理解。S5 必须要求
node/edge/condition/warning recall，并检查最终 citation 的 page/bbox/image_id；否则状态
为 `MISSING` 或 `BLOCKED`。

## 8. VLM Findings

- PDF figure VLM 通过 `get_tenant_default_model_by_type(..., LLMType.VISION)` 获取租户默认
  vision provider；没有配置时静默跳过（`figure_parser.py:102-114`）。
- 输入是已 crop 的 PIL image，叠加上下文 prompt；输出 description 追加到 figure text，
  随后走普通 tokenize/embedding。它不是整页 VLM，选择性范围是 figure/image。
- 并发由线程池（最多 10）和约 30 秒 timeout 控制；provider 延迟、费用和失败重试没有
  在 Enterprise parserApplication 中记录。当前没有稳定的 model/provider/version 字段，
  因此无法做版本可重复性或费用归因。
- H4 应保持 selective figure/diagram VLM，增加 provider/model/version、prompt hash、
  timeout/failure reason 的受限审计字段；不能在 Gateway 记录完整 prompt、图片或模型原文。

## 9. Chunk / Metadata

RAGFlow chunk 至少保留 `doc_id/kb_id/content_with_weight/position_int/page_num_int/top_int`
以及 `doc_type_kwd/img_id`；索引写入前图片上传到 MinIO，之后 `docStoreConn.insert`
（`chunk_service.py:179-228,331-385`）。`mom_id` 可形成 summary/parent 关系，但 Gateway
当前 citation 只暴露 `chunkId`，没有 parent-child 语义。

Enterprise 映射保存 equipment/fixed-asset/department/security/ACL 等文档级字段
（`app.py:470-504`），质量 worker 用 equipment/fixed-asset 作为 ground truth；不过这些
字段是否随每个 RAGFlow chunk 进入索引没有真实报告证明。S8 必须验证 metadata filter、
chunk 内 document/version 归属以及跨版本隔离。

## 10. Citation Traceability

RAGFlow public chunk list/get 已能返回 `content`, `document_id`, `image_id`, `positions`
（`chunk_api.py:461-575`）。Enterprise `_chunk_to_citation` 生成外部 documentId、
versionId、pageNo、bbox、chunkId、imageId、positions、evidence；positions 全量嵌入
`bbox.regions`，并由 `conversation_store` 保存。citation detail 在 ACL 后重新 GET
chunk evidence，RAGFlow 暂时不可用时回退到不可变快照（`formal_router.py:1049-1125`）。

缺口：快照没有原始对象的稳定下载 URI，imageId 也不是用户可读的 crop 地址；表格/图片
只有区域级 bbox，没有 cell/节点级引用；没有真实样本证明坐标系在不同 zoom/旋转页上仍
正确。正向 citation、负向 no-answer、source version mismatch 和 ACL 403/404 都是正式
验收硬门。

## 11. Quality Gate 风险与当前保证

质量 worker 从公共 document metadata 读取 `expected_tables/ground_truth_fields/
citation_expected/required_capabilities`，缺声明会将本来 `passed` 的结果降为
`review_required`（`quality/worker.py:477-534`）。query gate 对 pending/review/failed
fail-closed；`passed` 还必须满足：

1. `parserApplication.state == executed` 且 `readbackMatch == true`；
2. required capabilities 声明完整且每个对应 quality dimension 为 `passed`；
3. 正式报告中的指标、artifact hash、repeatability hash 和 ACL 证据完整。

版本提升要求 quality=`passed` 且 parser=`executed`；顺序为 enable new → SQLite
`BEGIN IMMEDIATE` promotion → disable superseded（`sync_service.py:93-130`，
`sync/models.py:721-766`）。SQLite 事务与 RAGFlow 状态不具备跨系统原子性；若 enable 成功
而 DB transaction 失败，需要 reconciliation/回滚，不能把一次成功调用当作最终一致性证明。

## 12. Real Sample Test Plan

正式 manifest 必须包含 16 个经人工复核、脱敏且带 SHA-256 的样本（S1–S6 各 2，S7a/S7b
各 1，S8 各 2），总查询不少于 50，其中负向 no-answer 不少于 5；每个样本至少一个
正向 citation 问题和一个不存在事实的问题。S7b 预期 `review_required`，其余预期
`passed`，但仍必须满足 parser executed 硬门。

| 场景 | 解析期望 | 正向检索/引用 | 失败条件 |
|---|---|---|---|
| S1 纯扫描正文 | 每页 OCR、关键设备字段、position | 事实答案 + 页/bbox | OCR CER > 0.05、字段缺失、坐标越界 |
| S2 扫描+表格 | table detection/structure/crop | cell 答案 + table region | table recall/cell accuracy/crop 失败；Markdown 要求不能默默降级 |
| S3 操作截图 | image crop/OCR/caption | 图中文字或 caption + image_id/bbox | 图不进 chunk、caption 缺失、引用漂移 |
| S4 设备示意图 | figure crop、标签 OCR | 设备标签/关系答案 + bbox | 只返回周围正文、无 image_id |
| S5 流程图 | 节点/箭头/分支/条件/warning | yes/no 路径答案 + 原图 bbox | generic OCR 冒充语义、edge/condition recall 不达标 |
| S6 混合 PDF | 原生文本与扫描页都覆盖 | 两类事实均可检索 | 任一页空 chunk、page coverage < 1 |
| S7a 旋转/低质量可接受 | 旋转纠正、降质但可读 | 可回答事实 + quality passed | orientation 或 degraded OCR 指标失败 |
| S7b 旋转/低质量不可接受 | 明确 review，不进入 current | no-answer/人工复核引用 | 低质仍 `passed` 或被提升为 current |
| S8 多页复杂手册 | chunk、metadata、版本隔离 | 设备号/固定资产号/故障码 + version | 跨版本泄漏、parent/child 或 metadata filter 错误 |

每个场景都必须做一次新解析和第二次新解析，比较 parse/e2e repeatability hash；不得
把同一次 run 的重复 GET 当成重复性证明。缺 manifest、样本文件、live 环境、RAGFlow
或 S3 时 runner 输出 BLOCKED/exit 2 或 external-env/exit 3，并留下原因和非敏感证据。

## 13. Hardening Implementation Breakdown

| 工作包 | 目标/修改范围 | 复杂度与依赖 | 并行性 | 验收保证 |
|---|---|---|---|---|
| WP03-H1 Parser Application | Enterprise routing、document PATCH/GET、terminal executed/mismatch、版本 promotion | M；依赖 RAGFlow public API | 可与 H2/H3 并行 | selected→configured→executed；调用顺序固定；mismatch/legacy 不 parse 通过；新版本未 passed 不得 current；重试无空窗 |
| WP03-H2 Scan/OCR/Layout | 保持 DeepDOC 主引擎，补页面级 OCR/fallback/rotation evidence，不自建 parser | M；依赖 H1、S1/S6/S7 | 可并行 | 每页 page coverage、OCR/CER、garbled/fallback/rotation 证据；S7b 必为 review |
| WP03-H3 Table & Figure Assets | 固化 HTML/Markdown 选择、table/figure crop hash、position/image_id、跨页关系 | M；依赖 H1、RAGFlow chunk API | 可并行 | S2 table cell/crop；S3/S4 image crop/OCR/caption；不把 image 周围正文冒充图内容 |
| WP03-H4 Selective VLM | 仅对 figure/diagram 调用 VLM，记录 provider/model/version/prompt hash/失败原因 | M；依赖 H3、租户 vision provider | 可并行 | S3–S5 caption/flowchart 指标；没有 vision model 时显式 not_applicable/review，不静默 passed |
| WP03-H5 Quality Gate | required declarations、parser executed、适用维度、promotion/reconciliation | M；依赖 H1、H2/H3 指标 | 可并行，最后集成 | 任一声明/维度/parser evidence 缺失不得 passed/current；warn 只允许明确 demo 模式 |
| WP03-H6 Retrieval/Citation E2E | public query/chunk API、ACL、正负查询、版本/坐标快照 | M；依赖 H1–H5、真实 corpus | 最后串行验收 | ≥50 queries、≥5 negatives、bbox IoU、version accuracy、ACL 403/404、两次 fresh parse hash 一致 |

实施顺序是 H1 → H2/H3/H4 并行 → H5 → H6。任何需要修改 RAGFlow 上游 parser、主
OpenAPI、v2 router/store 或鉴权的提议都必须另立 change request；本 WP-03 不扩大范围。

## 14. Critical / High / Medium Findings

### Critical

| Finding | 代码/证据 | 影响 | 建议 |
|---|---|---|---|
| C1 真实验收 corpus 缺失 | `enterprise/scripts/wp03/acceptance.py:228-300`；runner 需要 `artifacts/wp03/real-acceptance/manifest.json` | 没有真实 S1–S8 就无法声称 PROVEN，当前必须 BLOCKED | 提供 16 个脱敏人工复核样本、manifest、hash、query/ground truth；再跑新鲜 Contract/P0/WP03 |
| C2 跨 RAGFlow/SQLite promotion 非原子 | `sync_service.py:93-130`、`sync/models.py:721-766` | enable 成功而 DB 事务失败时可能短暂双 current/错误可见性 | 保留 enable-new→DB→disable-old 顺序，增加 reconciliation、失败回滚和审计告警；不要假设分布式事务 |

### High

| Finding | 代码/证据 | 影响 | 建议 |
|---|---|---|---|
| H1 Table Markdown 未实现 | `pdf_parser.py:1472-1483`、`nlp/__init__.py:473-505` | 只能保证 HTML/rows，无法满足 Markdown 契约 | 明确定义 HTML 为契约或增加最小、可追溯的 HTML→Markdown adapter，并加入 cell accuracy |
| H2 流程图语义缺失 | `pdf_parser.py:1311-1318`、`figure_parser.py:102-141,251-292` | generic figure/VLM 不能证明 branch/arrow/yes-no/warning | S5 以节点/边/条件/warning 指标验收；没有专用解析时保持 MISSING |
| H3 页面级 OCR 失败未完全上报 | `pdf_parser.py:771-779,1659-1661,1739-1742`；`sync_service.py:319-333` | run=DONE 可能包含空页/部分失败 | 输出每页 OCR/fallback/garbled/rotation 结果，质量门按适用场景阻断 |
| H4 VLM 可选且版本不可审计 | `figure_parser.py:102-114`、`quality/worker.py:99-112` | provider 未配置时静默跳过；无法复现 caption/费用 | 记录受限 provider/model/version/prompt hash 和 not_applicable/review 原因；不记录原文/图片 |
| H5 业务 metadata 进入 chunk 未证实 | `app.py:470-504`、`chunk_api.py:505-518` | 设备号/资产号可能只在 Enterprise 行，不在检索索引 | S8 验证 metadata filter 与 chunk 归属；必要时用公开 API 的 metadata 适配，不读上游内部 DB |

### Medium

| Finding | 代码/证据 | 影响 | 建议 |
|---|---|---|---|
| M1 PostgreSQL 集成不适用 | `enterprise/gateway/app.py:429` 使用 `aiosqlite`；无 PG repository 调用 | 单独 PG smoke 不能证明 Enterprise 行为 | runner 明确 `not_applicable`；若要支持 PG，另开 scope 实现 repository 与集成测试 |
| M2 citation 只有区域级证据 | `formal_router.py:250-290`、`conversation_store.py` citation 表 | 表格 cell、流程图 node 的精确回溯不足 | 先以区域 bbox 作为 v1 契约，S2/S5 失败则 review；不要伪造 cell/node 坐标 |
| M3 图像证据地址不稳定 | `chunk_service.py:179-228`、`ragflow_client.py:66-97` | image_id 可追踪但不是用户可下载 URI | 后续由受控对象存储服务生成短期授权 URL；保持浏览器不直连 MinIO 管理端口 |

## 验收保证与 runner 规则

统一 runner `enterprise/scripts/run_enterprise_tests.ps1` 的 Contract/P0/WP03/All profile
必须满足：JUnit `tests > 0`；失败、skip、xfail、xpass 均会失败；RAGFlow tree guard
前后均通过；每次运行生成独立 `run_id/summary.json`、环境矩阵和证据 hash。

- exit 0：测试和适用验收全部通过；
- exit 1：测试或已启动的验收失败；
- exit 2：正式 WP03 缺样本/manifest 或本地依赖，明确 BLOCKED；
- exit 3：live RAGFlow/S3/外部环境不可用；
- exit 4：runner、报告或 RAGFlow guard 自身异常。

本次收尾最后一次代码修改后必须重新执行 Contract、P0、WP03 三个 profile，并在交付
中给出新生成的绝对 artifact 路径。此前时间点的报告不再作为验收证据；真实 S1–S8
仍缺失时，WP03 结论保持 BLOCKED。
