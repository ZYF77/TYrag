# 解析质量评测契约

## 1. 目标

对真实类型 PDF 输出可重复、可复核、可验收的解析质量基线。评测结果服务于后续
OCR、表格、图片和 Chunk 优化决策，不替代 RAGFlow 解析引擎。

## 2. 状态

`parse_quality_status` 与 `sync_status` 相互独立：

```text
passed
review_required
failed
```

规则：

- `failed`：RAGFlow `run=FAIL`、Gateway 同步失败，或结果无法采集。
- `review_required`：RAGFlow `DONE` 但未通过质量阈值。
- `passed`：RAGFlow `DONE` 且全部阈值通过。
- RAGFlow `DONE` 不自动等同于 `passed`。

## 3. 指标

文档级：

1. 文档解析成功率：`DONE` 且可采集结果的比例。
2. 空页面比例：无 Chunk/Position 页码占源 PDF 页码比例。
3. 有效文本覆盖率：非空 Chunk 数 / Chunk 总数；无 Chunk 时为 0。
4. 乱码或异常字符比例：`U+FFFD`、`U+25A1` 等异常字符数 / 总字符数。
5. Chunk 数量和长度分布：数量、最小/最大/平均/P50/P95、空 Chunk 数。
6. 页码覆盖率：Chunk Position 覆盖的页码数 / 源 PDF 页码数；只统计
   `1 <= pageNo <= source_page_count` 的 Position。越界 Position 不计入
   covered pages，单独记录 `out_of_range_page_count` 和
   `out_of_range_pages`，越界页码不得掩盖真实空页。
7. 坐标覆盖率：含有效 Position 的 Chunk 数 / Chunk 总数。
8. 表格识别完整性：标注表格页码中检出表格 Chunk 的召回率。
9. 关键字段准确率：人工 ground truth 字段在 Chunk 文本中的命中率。
10. Citation 页码正确率：可选问题引用页码与 ground truth 页码一致的比例。
11. 单页和单文档解析耗时：RAGFlow `process_duration` 与按页均值。
12. 关键字段字符相似度：字段值与 Chunk 文本最佳模糊匹配的归一化相似度，
    用于量化 OCR 错字而不仅是字段是否逐字命中。

关键字段：

```text
equipment_id
fixed_asset_no
model
document_type
fault_code
version
effective_date
```

字段准确率必须以人工标注 ground truth 计算，不得用主观描述代替。

Phase 1 仅提供生成器同源的合成 ground truth（`ground_truth_provenance`
标记 `human_reviewed=false`），作为工程基线；合成 ground truth 的结果
不得称为人工标注准确率，也不能外推到客户真实扫描档案。

## 4. 数据来源

- 样本 Manifest：`enterprise/scripts/wp03/sample_manifest.json`
- Manifest 必须声明 `ground_truth_provenance.source` 与
  `ground_truth_provenance.human_reviewed`。
- 样本原文件：仅存在于 `artifacts/wp03/samples/`，不提交 Git
- 正式链路：MinIO/S3 -> Gateway `/documents` -> Outbox/Worker -> RAGFlow -> parsing
- 结果采集：RAGFlow 公共 API `documents`、`chunks`
- 禁止直接修改 RAGFlow 内部数据库

## 5. 输出

每次执行生成：

```text
artifacts/wp03/reports/<run_id>/evaluation-report.json
artifacts/wp03/reports/<run_id>/documents.csv
artifacts/wp03/reports/<run_id>/pages.csv
artifacts/wp03/reports/<run_id>/chunks.csv
artifacts/wp03/reports/<run_id>/baseline.md
```

报告必须能定位到文档和页面。

每次报告的环境信息必须记录：

```text
enterprise_commit
enterprise_worktree_dirty
ragflow_source_tag
ragflow_source_commit
manifest_digest
thresholds_digest
evaluation_contract / evaluation_contract_version
执行时间和执行命令
```

正式 baseline 只在工作树干净时生成；工作树不干净时必须拒绝运行，或使用
`--allow-dirty` 显式标记为非正式、不可复现结果，不得默认生成正式 baseline。

`metrics_hash` 用于完整产物一致性；`parse_repeatability_hash` 用于比较同一样本
两次 fresh parse 的解析语义结果（Chunk 文本、页码、坐标、表格、关键字段），
`e2e_repeatability_hash` 进一步纳入 Citation、`quality_status`、`quality_reasons`
和 `sync_status`。两个 hash 都必须排除 dataset/document/task/event/run ID、
时间戳、解析耗时和临时路径，并保留 `sample_id`、`file_sha256`、规范化 Chunk
文本、页码、坐标、表格结果、关键字段结果、质量指标和 reason codes。
报告同时提供 `artifact_hash` 校验报告文件完整性。

基于已保存 fresh-parse 结果重新计算 hash 时，报告必须记录原始解析 Run ID、
recompute commit、是否发生重新解析（`reparsed=true/false`）。

## 6. 阈值

默认阈值见 `enterprise/scripts/wp03/thresholds.json`。阈值配置化，不允许在执行后
临时降低以通过验收；本阶段只建议基线，最终阈值由客户样本和风险确认。
阈值文件必须声明 `threshold_version`、`phase` 和
`temporary_conservative`；当前 1.0 阈值仅用于 Phase 1 合成样本工程基线。

## 7. WP-03 有效解析执行契约

质量报告只有在能证明“选了什么、配置了什么、实际执行了什么、读回是否一致”时
才可用于版本晋级。每个文档必须持久化以下状态之一：

```text
selected -> configured -> executed
                     \-> mismatch
legacy_unverified
```

- `selected`：Gateway 根据受控 profile 选择了 parser profile；PDF 的安全默认是
  `naive` + `layout_recognize=DeepDOC`，不得按业务类别把 PDF 直接路由为
  `picture` 或 `table`。
- `configured`：仅在 RAGFlow 文档仍为 `UNSTART` 时，通过 public API 写入配置，
  然后 GET 回读并比较 Gateway 所拥有的字段子集；缺字段、值不一致或 PATCH 被拒绝
  均为 `mismatch`。
- `executed`：解析启动后，必须 GET 到终态（`DONE`/失败终态）并保存终态、执行
  profile、配置摘要和 API 请求关联信息。仅有 `DONE` 不等于质量通过。
- `legacy_unverified`：历史文档没有上述证据，必须可查询并在 enforce 模式阻断
  晋级，不得用默认值补写成已验证。

质量评估必须带 `required_capabilities`。适用维度的值为 `null`、
`not_evaluated` 或证据缺失时，enforce 模式只能产生 `review_required`/`failed`，
不能产生 `passed`。仅不适用的维度允许为 `not_applicable`。

## 8. 版本激活和引用证据不变量

新版本在解析和质量门禁通过前 `current_version=0`，旧 current 版本保持 enabled。
晋级顺序固定为：确认新文档 enabled → SQLite `BEGIN IMMEDIATE` 事务内把唯一
 current 指向新版本 → 事务成功后再禁用旧 RAGFlow 文档。任一步失败都必须可重试，
 且不能留下“无 current”或新旧版本都被查询的状态；旧版本保留为可审计的
 `superseded`/`legacy_unverified` 记录。

RAGFlow chunk position 的原始格式是 `[page, left, right, top, bottom]`。Gateway
 必须保留所有 positions，并按 `pageNo=page`、`bbox={left,right,top,bottom}`
 映射；不得只取第一个或把坐标顺序改成 y1/y2。citation 的页码、版本和授权检查
 独立于消息业务状态；引用缺失不能把 `completed` 改判为其他状态。
