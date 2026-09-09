# P0 落地说明：RAGFlow → Gateway document-run terminal webhook

日期：2026-09-04（Asia/Shanghai）

## 已落地

- RF-PATCH-010：终态推送（DONE/FAIL/CANCEL），仅 `run` 值变化时发送；无 RUNNING 进度垃圾。
- Gateway：`POST /enterprise/api/v1/internal/ragflow/document-run-terminal`
  - HMAC：`X-Enterprise-Timestamp` + `X-Enterprise-Signature`（复用 `callback.sign_payload` / `verify_signature`）
  - 内网限制：`ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_TRUSTED_CIDRS`（默认 RFC1918 + loopback；**无 mTLS**）
  - inbox：`ragflow_status_inbox.event_id` UNIQUE 幂等
  - `SyncService.apply_ragflow_run` 与 poll 共用；质检 / EAM callback 既有幂等兜底
  - `ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_IGNORE_CANCEL` 默认 `true`
- StatusReconciler **保留**（双轨）；未降频。
- 未改 Gateway→EAM callback 契约；未做 RF outbox 产品化。

## 配置旋钮

**RAGFlow**

- `ENTERPRISE_STATUS_WEBHOOK_ENABLED`（默认 false）
- `ENTERPRISE_STATUS_WEBHOOK_URL`
- `ENTERPRISE_STATUS_WEBHOOK_SECRET`（与 Gateway 同一全局 secret）
- `ENTERPRISE_STATUS_WEBHOOK_TIMEOUT_MS`（默认 2000）
- `ENTERPRISE_STATUS_WEBHOOK_MAX_ATTEMPTS`（默认 3）

**Gateway**

- `ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_ENABLED`（默认 false）
- `ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_SECRET`
- `ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_IGNORE_CANCEL`（默认 true）
- `ENTERPRISE_RAGFLOW_STATUS_WEBHOOK_TRUSTED_CIDRS`

## eventId

`sha256(doc_id + "|" + runCode)`，避免同态 update_time 抖动与双 FAIL 钩重复 inbox。
