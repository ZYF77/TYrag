# CHANGE REQUEST：RF-PATCH-010 文档解析终态 Webhook

## 原因

Gateway 解析状态主路径要从轮询改为 RAGFlow→Gateway 推送；公开 list API 的权威终态在文档 `run`（DONE/FAIL/CANCEL），需在 RF 写库后通知 Gateway。EAM 回调仍只由 Gateway 发出。

## 最小上游修改

- `ragflow/api/utils/enterprise_status_webhook.py`
  - 组装 `ragflow.document.run.terminal`、HMAC 签名、短超时 HTTP POST（可后台线程）。
- `ragflow/api/db/services/document_service.py`
  - `_sync_progress`：仅当 `run` **值变化**进入 DONE/FAIL/CANCEL 时发送。
  - `update_by_id`：覆盖 CANCEL 等经 `update_by_id` 写入的终态。
- `ragflow/api/db/services/task_service.py`
  - `update_progress(progress==-1)` 写 Document FAIL 成功后发送（辅钩）。

## 安全与兼容性

- 默认 `ENTERPRISE_STATUS_WEBHOOK_ENABLED=false`。
- 不直连 EAM；无 RF 出站 outbox 产品化（P0）；漏发靠 Gateway StatusReconciler。
- 发送失败不影响 parse 写库；401/403 不重试。

## 测试

- `enterprise/tests/test_ragflow_status_webhook.py`（验签、inbox 幂等、仅 run 变化发送、默认忽略 CANCEL、内网 CIDR）。

## 回滚

移除上述 hook 调用与模块；关闭 Gateway `ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_ENABLED`；保留 StatusReconciler 即可回到纯轮询。
