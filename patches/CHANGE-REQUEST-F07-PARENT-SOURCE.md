# F07 — 父块来源隔离最小上游补丁

状态：本地实现、无服务验证通过，真实引擎/解析集成未验收；未部署、未重解析生产数据。

写入入口：`rag/svr/task_executor.py::insert_chunks`、`rag/svr/task_executor_refactor/chunk_service.py::_create_mother_chunks`。
共享实现：`rag/utils/parent_chunks.py`。读取入口：`rag/nlp/search.py::Dealer.retrieval_by_children`。
聚合接入：Chat dialog_service、Workflow Retrieval、Agentic RAGTools与harness搜索。

新ID绑定租户/知识库/文档/解析任务摘要/父块文本；查询前限定ID、知识库、文档，返回后摘要校验。旧ID一律保留原子块，不丢弃可用子证据。F02的G/S范围约束保持不变。本项不修复通用ES get的其他调用路径，也不改变引擎抽象或官方数据库迁移。

验证命令：
```
python3 -m unittest discover -s docs/reviews/tests -p test_f07_parent_sources.py -v
python3 docs/reviews/2026-09-20-audit-probes.py
```
7项父块测试覆盖跨文档/租户/知识库/任务、重复解析、旧/缺失/错误父块、共享Dealer和重构入口及聚合。使用合成内存store，不能替代真实ES/Infinity契约。Python3.13完整环境、普通任务完整解析调用链、真实引擎端到端及生产历史重建尚未验收。

升级重放：检查两个写入入口是否已被上游合并或替换、mom_id字段容量、search(id/kb_id/doc_id)条件及get_fields的键语义；不能仅重放hash函数而遗漏读取校验。独立提交，不混入F06。

## 历史盘点与重建

1. 先部署安全读取，再部署两个新写入入口；旧块回退子块期间可用性与召回长度可能变化。
2. 经环境授权的操作员导出指定文档的JSONL索引记录，包含原始ID、doc_id/kb_id、mom_id及父块内容；文件只留受控工作区，不上传、不提交。
3. 用 `enterprise/scripts/inspect_parent_chunks.py --input <导出文件> --tenant-id <租户> --dataset-id <库> --document-id <文档>` 只读统计，输出仅计数。
4. 在既有RAGFlow文档解析入口对明确文档逐批重解析，保持Gateway版本映射；不要直接重命名旧记录。等待完成后再盘点并验证实际检索/引用位置。失败批次保持子块回退，按既有失败重试流程处理。
5. 旧父块清理另行评估，禁止本次自动清理共享旧ID。回滚不恢复纯文本hash读取。
