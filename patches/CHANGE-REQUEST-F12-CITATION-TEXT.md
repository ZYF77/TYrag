# F12：保护技术正文，保留旧引用兼容

状态：企业后端/前端实现与合成样例已完成；固定环境 HTTP/数据库契约验收待执行；未部署。

## 成功标准与契约

继续支持明确 [ID:n]、已知 ID 损坏形式和普通中文正文 [n]。arr[0]、matrix[1][2]、[L1]、[警告]、[]、空白和时间片段保持原样，时间片段不提取引用。支持原有 ASCII / Arabic-Indic / Extended Arabic-Indic 数字。

源文本扫描器先保护围栏/缩进代码、行内代码、链接/图片（含嵌套方括号和 URL 括号）、自动链接、已定义的引用式链接及定义行、转义方括号，再在普通正文处理引用。不序列化 Markdown，不删除未知括号、不折叠空白。后端裸 ID:n 提取也遵守保护区域；前端沿用原来的角标绑定规则，仅替换认可的标记。

## 独立变更单元

- `enterprise/gateway/query/citation_select.py`：共享清洗/提取保护扫描器与旧数字引用边界；保留引用编号、稀疏 ID 映射、正文重叠兜底和业务状态算法。
- `enterprise/web/src/components/common/citationText.ts` 与 `CitationMarkdown.tsx`：镜像保护/修复规则，沿用现有 ReactMarkdown 和角标绑定，无新增依赖。
- `enterprise/tests/fixtures/citation-text-cases.json`：前后端共用的合成输入、预期正文、预期编号、预期 Markdown 链接。
- `enterprise/tests/test_sanitize_citation_markers.py`、`test_citation_select.py` 与前端 `CitationMarkdown.test.tsx`：幂等性、保护区域、稀疏引用和状态独立性；原有删除括号/猜测时间的断言改为保留正文。原 Workflow 导入测试保留，在其测试内导入路由，以便独立文本测试不依赖服务启动。
- `enterprise/tests/test_v2_conversation_contract.py` 中新增的 `test_technical_citation_body_survives_response_and_history`：Chat/Workflow × JSON/SSE，重建最终 SSE 正文并与历史一致性比较，代码/链接 ID 不产生引用。
- 完整审查报告和审查探针中的 F12 部分。

## 验证与集成

详细执行结果以完整审查报告 F01/F12 验证表为准。前端单测/TypeScript 检查和可独立运行的 Python 文本测试可执行；宿主为 3.14.4，缺固定服务依赖，不能据此宣称 Python 3.13、PG 或完整 JSON/SSE 集成验收通过。

固定环境命令：

```sh
python -m pytest enterprise/tests/test_sanitize_citation_markers.py enterprise/tests/test_citation_select.py enterprise/tests/test_v2_conversation_contract.py -q
cd enterprise/web
npm test -- src/__tests__/CitationMarkdown.test.tsx src/__tests__/Citations.test.tsx
./node_modules/.bin/tsc -b --pretty false
```

无 OpenAPI/schema/认证配置/Workflow/锁文件变化，未修改 RAGFlow 上游，不占用 RF-PATCH 编号。部署时配套更新 Gateway 与企业前端，否则旧前端仍可能误改正文。已持久化正文不重写，旧版本已删除的文本无法恢复。扫描器是局部保守识别，不取代完整 Markdown 渲染器；后续扩展须同时增加 Python/TS 共用样例。ACL 继续暂缓，F05 与引用定位不纳入本次修复。
