# Workflow 通用诊断：变更与验证记录

日期：2026-09-20。实现已完成；真实部署环境验收尚未执行。

## 修改文件与行为

- `ragflow/rag/diagnostics.py`、新增 `ragflow/rag/workflow_diagnostics.py`：请求 sink、执行
  父子关系、同步线程上下文、延迟流式计时、checkpoint、异常/关闭清理。
- `ragflow/agent/canvas.py`、`ragflow/agent/tools/base.py`：统一节点/内置工具/MCP 调度埋点。
- `ragflow/api/apps/restful_apis/agent_api.py`、`ragflow/api/db/services/canvas_service.py`：
  新会话与继续会话的 JSON/SSE 诊断返回、失败时保留 checkpoint。
- `enterprise/gateway/query/workflow_client.py`、`workflow_router.py`：失败响应先合并私有
  诊断；节点/工具错误摘要不保存异常正文。
- `enterprise/web/src/components/console/RagDiagnosticsPanel.tsx`、
  `enterprise/web/src/styles/rag-diagnostics.css`：直接展示节点身份/用途、工具、模型、执行
  关联、状态、耗时和截断提示；复用现有 Dialog，长 ID 自动换行。
- 测试：新增 `ragflow/test/unit_test/rag/test_workflow_diagnostics.py`、
  `test_workflow_diagnostics_contract.py`、
  `enterprise/web/src/__tests__/WorkflowDiagnostics.test.tsx`；增强
  `enterprise/tests/test_query_diagnostics.py`。
- 登记：`decisions/ADR-022-Workflow通用执行诊断.md`、
  `patches/CHANGE-REQUEST-RF-PATCH-013-WORKFLOW-DIAGNOSTICS.md`、`patches/manifest.yaml`。

## 已执行的验证

测试依赖安装到 `/tmp/tyrag-workflow-venv` 及前端 `node_modules`，未修改依赖锁文件。
Python 3.14 环境下，线程池单测需要在沙箱外运行，否则 asyncio 的线程完成通知停在
selector 等待；沙箱外 26 项测试均正常完成。

```bash
PYTHONPATH=ragflow:. /tmp/tyrag-workflow-venv/bin/python -m pytest \
  --noconftest -c /dev/null -o cache_dir=/tmp/tyrag-pytest-cache \
  ragflow/test/unit_test/rag/test_workflow_diagnostics.py \
  ragflow/test/unit_test/rag/test_workflow_diagnostics_contract.py \
  ragflow/test/unit_test/rag/test_diagnostics.py -q
# 26 passed；2 个上游 asyncio.iscoroutinefunction 的 Python 3.14 弃用提示。

PYTHONPATH=ragflow:. /tmp/tyrag-workflow-venv/bin/python -m pytest \
  enterprise/tests/test_query_diagnostics.py \
  enterprise/tests/test_workflow_client_timeout.py -q
# 18 passed；包括 runId 隔离、错误 checkpoint、去重及非管理员权限拒绝。

cd enterprise/web
npm run build
npm test -- src/__tests__/WorkflowDiagnostics.test.tsx \
  src/__tests__/SystemSettingsPanels.test.tsx -t 'diagnostic|future node'
# 构建通过；选中的 2 项诊断用例通过，其他 23 项未被 -t 选择。
```

上游测试使用 `--noconftest` 是因为这些测试不依赖全局 parser/模型初始化；不是删除断言
或跳过失败。API/工具契约测试执行从生产文件提取的原函数体，以合成依赖代替数据库和
模型，不能视为真实服务 E2E。

另执行整份 `SystemSettingsPanels.test.tsx` 与新增测试：24 passed、1 failed。
失败是 `combines conversation advanced filters and applies them server-side`，提交的
businessUserId 不完整。临时恢复 HEAD 版诊断组件后，整份原测试仍是 23 passed、1 failed，
同一高级筛选用例失败；随后已恢复本次实现。未删除测试或弱化断言。

Python 修改文件编译、`git diff --check`、补丁清单 YAML 解析均通过。

## 契约、配置与集成

- 有上游修改，已登记 RF-PATCH-013；没有数据库/对象存储、迁移、公开 OpenAPI 或新依赖。
- 私有 diagnostics v1 只增量增加事件/字段；沿用既有开关、管理员权限和租户隔离。
- Gateway 公共回答状态/citations 规则不变；旧运行不会自动补齐新诊断。
- 本终端没有 Docker，也没有可用的 PostgreSQL/模型运行栈；未执行真实 PostgreSQL 集成、
  浏览器 E2E、容器重启、30 服务器部署。前端 jsdom 测试不替代浏览器 E2E。
- 集成时同步更新 RAGFlow、Gateway 与企业前端，重启对应 Python 服务并发布前端构建。
  保持诊断开关开启，用非敏感合成问题分别测试普通 Chat、Workflow JSON、Workflow SSE、
  继续会话、循环/并行节点、Agent 内工具/MCP、工具失败；核对节点关联、耗时、失败记录、
  公共响应无 `_diagnostics`、非管理员 403 和跨租户 404。
- 自动覆盖范围是 Canvas/统一工具入口及已有阶段埋点；自定义节点绕过入口的内部操作
  需要自行记录安全阶段。保留 256 事件/256 KiB 上限，截断时 UI 明示；断网或进程退出
  后无法恢复尚未发送的 checkpoint。

原有 `enterprise/tests/fixtures/Doc1.pdf` 和 `docs/reviews/` 工作区改动未修改。
