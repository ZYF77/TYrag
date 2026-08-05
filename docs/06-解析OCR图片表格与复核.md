# 06 解析、OCR、图片、表格与复核

## 1. 原则

RAGFlow 负责主解析链路。本项目只做：

- 解析器 profile 选择；
- 外部 OCR/VLM Provider 适配；
- 质量检测；
- 业务字段抽取；
- 人工复核和发布门禁。

不重新实现通用 `pypdfium2 + pdfplumber + OCR + chunking` 平台。

## 2. 文档类型

P0 必须验证：

- 单栏数字 PDF；
- 双栏数字 PDF；
- 纯扫描 PDF；
- 同页数字和扫描混合 PDF；
- 含表格 PDF；
- 含流程图、设备示意图和截图 PDF；
- 旋转页；
- 加密、损坏、超大和超页数 PDF。

## 3. 解析路由

建议 profile：

| 条件 | Parser 路线 |
|---|---|
| 原生文本充足、版面简单 | 原生/Naive parser |
| 扫描或 OCR 主导 | DeepDoc 或经批准的 OCR/VLM |
| 复杂版面、长 PDF | MinerU、Docling、OpenDataLoader，按固定版本评测 |
| 高价值复杂表格 | 专业表格解析 Provider 或人工复核 |
| 低置信度/结果异常 | review_required |

解析器选择必须基于真实样本评测，不以“功能更多”作为唯一依据。

## 4. 图片和流程图

要求：

- 保留文档、版本、页码和 bbox；
- 图片 OCR、标题、相邻正文和描述进入检索文本；
- 原图/裁剪图经 ACL 后才能返回或发送多模态模型；
- 流程图回答必须保留条件分支、否定条件和警告；
- 无多模态核验时，不得仅凭历史描述编造图中步骤。

## 5. 表格

MVP 保存和返回：

- 表格所在文档、页码和位置；
- 可用的 Markdown/HTML/结构化结果；
- 表格标题和上下文；
- 表格截图或原页证据。

只有明确存在统计需求的稳定表型，才进入业务 PG 专用表。不能将所有表格一律结构化入业务库。

## 6. 质量字段

企业映射库保存：

```text
parser_profile
parser_version
ocr_model_profile
parse_started_at
parse_completed_at
page_count
chunk_count
asset_count
failed_pages
quality_status
review_status
review_reason
```

## 7. 发布门禁

以下情况不得自动 ready：

- 解析任务失败；
- 关键页面为空或明显乱序；
- 强制 metadata 缺失；
- 设备关联冲突；
- 关键表格/流程图要求但未提取；
- 上游文件已停用；
- 权限字段非法；
- 解析结果数量与状态不一致。

MVP 可先文档级复核；逐页检查点和页级人工处理放入 Beta，除非真实样本证明文档级复核无法满足验收。
