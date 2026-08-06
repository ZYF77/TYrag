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

`repeatability_hash` 排除 dataset/document/task/event/run ID、时间戳、耗时等
运行时字段，并纳入规范化 Chunk 文本、页码和坐标，因此可用于比较两次
fresh parse 的语义结果。`metrics_hash` 与报告内的 `artifact_hash` 用于校验
完整产物一致性。

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
