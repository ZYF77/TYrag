# RF-PATCH-012 Change Request

## 原因

原生 Agent Workflow Retrieval 没有 Gateway 文档授权集合的硬边界；当前多格式
Ingestion 还需要在画布 Parser 中使用现有 JsonParser 处理 JSON/JSONL。

## 替代方案

曾考虑只把 `authorized_doc_ids` 放入 Begin 提示词，或让 Gateway 在回答后删掉
越权引用。这两种方式都不能阻止检索阶段访问越权内容，因此不采用。

## 最小修改

- Retrieval 仅新增 opt-in `doc_scope_ids`/`doc_scope_mode=restrict`；
- Parser 仅新增 JSON/JSONL setup 与 JsonParser 分支；
- 默认 Chat、Parser 和 Dataset 行为保持不变。

## 兼容与升级

修改属于上游 RAGFlow Python 源码，需单独构建镜像、登记 source commit，并在
升级时重放或删除本补丁。Gateway 只有在新镜像和受控 Workflow 版本同时配置后
才启用第二入口。

## 验收

覆盖空 scope、范围外文档、metadata 缩小、父子块、嵌套 JSON、JSONL、0/false
值和普通路径回归。禁止用跳过测试或伪造结果通过。

## F02 范围处理修正

`doc_scope_mode=restrict` 下，Metadata Filter 得到空集合时必须在 Retrieval
调用前短路，并输出空 `formalized_content`/`json=[]`；不得把空 `doc_ids` 传给
ES 以隐式扩大检索。有效范围始终是 Gateway 集合 G 与 metadata 结果的交集 S。
没有 metadata 条件时仍使用 G。非 `restrict` 调用保持 RAGFlow 原有的回退行为。

该修正不改变生产 Workflow 的 Research/AuthorizedEvidence 补检索流程。补检索
仍由工作流显式决定，并且每个补检索工具继续携带同一个 Gateway 文档范围。升级时
需要验证 Target 零命中不会调用检索，而显式 AuthorizedEvidence 仍能在 G 内检索。

对应回归测试：

- `ragflow/test/unit_test/agent/tools/test_retrieval_scope.py`：manual/auto/semi_auto
  零命中短路、交集传递、空条件正常检索、重复调用清空旧输出和 legacy 兼容；
- `docs/reviews/2026-09-20-audit-probes.py`：受限零命中不调用检索器的源码级探针。
