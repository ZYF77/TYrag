# WP-03 解析质量评测

## 用途

对真实类型 PDF 建立可重复的解析质量基线。样本文件只写入
`artifacts/wp03/samples/`，不提交 Git；Manifest 只包含非敏感元数据和
生成器同源的 Phase 1 工程 ground truth（`human_reviewed=false`），
不是独立人工标注；人工标注在真实客户样本阶段补充。

## 目录

```text
sample_manifest.json      脱敏样本 Manifest
generate_samples.py       确定性样本 PDF 生成器
thresholds.json           可配置质量阈值
metrics.py                指标计算
quality_gate.py           parse_quality_status 门禁
collector.py              S3 -> Gateway -> RAGFlow 公共 API 采集
report.py                 JSON/CSV/Markdown 报告
run_parsing_evaluation.py CLI 执行入口
```

## 生成样本

需要 reportlab/PIL；可用桌面端自带 Python：

```powershell
& 'C:\Users\Lemon\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  enterprise/scripts/wp03/generate_samples.py `
  --output-dir artifacts/wp03/samples `
  --manifest enterprise/scripts/wp03/sample_manifest.json `
  --write-manifest
```

## 执行真实评测

要求环境变量：

```text
RAGFLOW_BASE_URL
RAGFLOW_API_KEY
GATEWAY_URL
ENTERPRISE_SYNC_SERVICE_TOKEN
S3_ENDPOINT
S3_ACCESS_KEY
S3_SECRET_KEY
S3_BUCKET
```

```powershell
python enterprise/scripts/wp03/run_parsing_evaluation.py `
  --run-id wp03-phase1-full-v1
```

报告输出到 `artifacts/wp03/reports/<run-id>/`。

需要证明“同一样本重新解析可复现”时，使用 `--fresh-parse`：每次执行使用
run 级 tenant 和 external document id，创建新的 RAGFlow dataset，避免复用
上一次已解析的文档：

```powershell
python enterprise/scripts/wp03/run_parsing_evaluation.py `
  --run-id wp03-phase1-repeat-a `
  --only wp03-digital_text-001 `
  --skip-citations `
  --fresh-parse
```

`parse_repeatability_hash` 排除 dataset/document/task/event/run ID、时间戳、
耗时等运行时字段，并纳入 `sample_id`、`file_sha256`、规范化 Chunk 文本、
页码和坐标，用于比较两次 fresh parse 的解析语义结果；
`e2e_repeatability_hash` 额外纳入 Citation、`quality_status`、`quality_reasons`
和 `sync_status`，用于比较端到端可复现性。`metrics_hash` 与报告内的
`artifact_hash` 用于校验完整产物一致性。

基于已保存 fresh-parse 结果重新计算 hash 而不重新解析时，使用：

```powershell
python enterprise/scripts/wp03/recompute_hashes.py
```

重算后的报告会记录原始解析 Run ID、recompute commit 和
`reparsed=false`。

正式 baseline 只在 Git 工作树干净时生成；报告会记录 `enterprise_commit` 和
`enterprise_worktree_dirty`。工作树不干净时默认拒绝运行，使用 `--allow-dirty`
可生成并明确标记为非正式、不可复现结果。

## 状态与阈值

`parse_quality_status` 独立于 WP-02 `sync_status`：

```text
passed
review_required
failed
```

RAGFlow `DONE` 不自动等于 `passed`。默认阈值见 `thresholds.json`，可在 CLI
通过 `--thresholds` 覆盖。当前阈值标记为 Phase 1 临时保守阈值
（`temporary_conservative=true`），不作为客户验收阈值。

## 正式 S1-S8 验收

合成样本只能作为工程回归，不能替代正式验收。统一入口为：

```powershell
pwsh -File enterprise/scripts/run_enterprise_tests.ps1 -Profile WP03
```

正式样本默认从 `artifacts/wp03/real-acceptance/` 读取，也可通过
`WP03_ACCEPTANCE_MANIFEST` 和 `WP03_ACCEPTANCE_FIXTURE_DIR` 指定。Manifest
必须至少包含 16 份真实脱敏样本：S1-S6、S8 各至少 2 份，S7a/S7b 各至少
1 份，并提供至少 50 道查询（至少 5 道为人工标注的无答案问题）。每个正向
引用问题必须声明 `expected_answer_contains`、页码和合法 `expected_bbox`；
每个样本至少声明一个负向问题。每个样本声明 `scenario_id`、`file_sha256`、
`acceptance_dimensions`、人工标注字段、引用问题和期望质量状态；
`ground_truth_provenance.human_reviewed` 必须为 `true`，且不能来自
`synthetic_generator`。

S1-S8 分别覆盖扫描文本、扫描表格、操作截图、设备图、流程图、混合 PDF、
旋转/退化扫描和多页手册。真实脱敏样本或在线环境缺失时，runner 生成
BLOCKED JUnit 和 evidence，退出码为 2；不得以 skip 或自动生成合成 PDF
形成正式绿色报告。

每次执行的 evidence、能力矩阵、JUnit 和解析报告写入
`artifacts/enterprise-tests/<run-id>/`。报告只记录端点的脱敏地址和凭据是否
存在，不记录 Token、密钥或客户正文。Enterprise 当前持久化调用链为
SQLite，因此报告明确记录 PostgreSQL 为 `not_applicable`，不运行无意义的
裸 PostgreSQL smoke test。
