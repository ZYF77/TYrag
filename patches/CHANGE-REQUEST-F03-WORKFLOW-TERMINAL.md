# F03：Workflow 终态协议收紧

状态：企业层代码已实现；固定 Python 3.13、PostgreSQL、真实脱敏 Workflow 验收待执行；未部署。

## 成功标准

- JSON 与 SSE 统一消费 Agent SSE；收到有效 `workflow_finished` 前不得成功。
- `message_end.status` 作为业务状态来源；usage-only 结束事件不覆盖它，明确 failed 单调保持。
- 缺终态/EOF/取消返回 `RUN_INTERRUPTED`；畸形、缺失或冲突状态返回 `RAGFLOW_API_INCOMPATIBLE`。
- 节点错误可以在合法后续终态恢复，并保留有界诊断；不能用正文、拒答句或 citations 数量推导状态。
- Gateway 授权范围为空的路径显式返回 `no_reliable_evidence`，不要求不存在的上游事件。

## 变更文件与边界

- `enterprise/gateway/query/workflow_events.py`：新增事件收集器和协议错误分类。
- `enterprise/gateway/query/workflow_client.py`：JSON `complete()` 改为消费 `stream()`，保留旧 JSON envelope；Stub 同步经过收集器。
- `enterprise/gateway/query/workflow_router.py`：JSON/SSE 接入收集器，删除文本/证据状态推断，处理空 Gateway 范围。
- `enterprise/tests/test_workflow_events.py`、`docs/reviews/2026-09-20-audit-probes.py`：合成事件契约测试和探针。
- 完整审查报告与 ADR-023。

未修改 RAGFlow 上游、生产 Workflow/提示词、OpenAPI、数据库 schema、依赖锁或配置；不占用 RF-PATCH 编号。F04 保存/展示、F05 并发、F07 父块来源和 F08 ACL 保持独立。

## 验证

```sh
python3 -m py_compile enterprise/gateway/query/workflow_events.py \
  enterprise/gateway/query/workflow_client.py \
  enterprise/gateway/query/workflow_router.py
python3 -m pytest enterprise/tests/test_workflow_events.py -q
python3 docs/reviews/2026-09-20-audit-probes.py
```

当前宿主缺 `pytest_asyncio` 等固定依赖，测试 collection 未执行；探针通过仅证明源码级合成不变量。固定环境还需用真实上游事件形状验证缺终态、取消、异常分支和 JSON/SSE 历史一致性。

## 升级与回滚

升级时检查 Agent SSE 事件 envelope、`message_end`/`workflow_finished` 状态字段和暂停事件；事件协议变化应先更新收集器与脱敏样例。回滚本变更文件即可回到原客户端解析，不需要迁移；生产部署另行执行。
