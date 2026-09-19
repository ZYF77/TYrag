# EAM User Long-term Memory — Phase 1

## Config knobs (Gateway)

| Env | Default | Meaning |
|---|---|---|
| `ENTERPRISE_USER_MEMORY_ENABLED` | `false` | Feature gate. When false, Search/Candidate are no-ops; Q&A unchanged. |
| `ENTERPRISE_MEMORY_ID` | empty | Fixed enterprise SEMANTIC Memory pool id in RAGFlow. Required when enabled. |
| `ENTERPRISE_USER_MEMORY_TOP_N` | `5` | TopK for `GET /api/v1/messages/search` (1–20). |
| `ENTERPRISE_USER_MEMORY_TIMEOUT` | `5.0` | Seconds for Memory HTTP calls. |
| `RAGFLOW_API_KEY` / `RAGFLOW_BASE_URL` | existing | Bearer auth for Memory APIs (same as Chat). |

Missing `ENTERPRISE_MEMORY_ID` while enabled → warn once, Memory no-op, Q&A continues.

## Subject

`subject = eam:{tenant_id}:{business_user_id}` from `UserPrincipal` only. Never from client body/query.

## P0 RF auth fix

`ragflow/api/apps/restful_apis/memory_api.py` trusts client `user_id` when:

- `getattr(g, "auth_type", None) == AUTH_API` (primary; Python API Key path), or
- `auth_via_api_token` is set (compat with Go middleware).

JWT/session callers always use `current_user.id`.

### Curl A/B isolation (API Key)

Replace host / key / memory id / subjects.

```bash
# Write candidate for user A
curl -sS -X POST "$RF/api/v1/messages" \
  -H "Authorization: Bearer $RAGFLOW_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "memory_id": ["'"$ENTERPRISE_MEMORY_ID"'"],
    "agent_id": "chat:demo",
    "session_id": "sess-a",
    "user_id": "eam:tenant1:userA",
    "user_input": "I prefer short answers",
    "agent_response": "Noted."
  }'

# Search as A — should hit
curl -sS -G "$RF/api/v1/messages/search" \
  -H "Authorization: Bearer $RAGFLOW_API_KEY" \
  --data-urlencode "memory_id=$ENTERPRISE_MEMORY_ID" \
  --data-urlencode "query=short answers" \
  --data-urlencode "user_id=eam:tenant1:userA" \
  --data-urlencode "top_n=5"

# Search as B — must be empty / not include A's memory
curl -sS -G "$RF/api/v1/messages/search" \
  -H "Authorization: Bearer $RAGFLOW_API_KEY" \
  --data-urlencode "memory_id=$ENTERPRISE_MEMORY_ID" \
  --data-urlencode "query=short answers" \
  --data-urlencode "user_id=eam:tenant1:userB" \
  --data-urlencode "top_n=5"
```

JWT UI session must **not** accept a spoofed body `user_id` (forced to owner tenant user id).

## Chat prompt upgrade (prod — do not auto-deploy)

Marker bumped to `enterprise_identity_metadata_v14` with optional `{user_memory}` parameter.
Existing Chats are **not** auto-patched (`_ensure_chat` never overwrites operator prompts).
After enabling Memory on prod Gateway, operators should upgrade Chat `prompt_config` once
(or recreate the enterprise Chat) so `{user_memory}` is substituted. Until then,
`needs_enterprise_prompt_upgrade(chat)` is true and injection is a no-op at the prompt layer
even if the Gateway sends `user_memory` in the completion body.

## P2 skeleton

`GET/DELETE /enterprise/api/v1/ai/memory/me` and `PATCH .../me/{id}` (501 for PATCH).
Subject from Token only; query/body userId rejected.


## Console / hot-reload (runtime `userMemory`)

Gateway Console **System Settings → Integrations → Gateway runtime** exposes section `userMemory`
(UI label: 用户长期记忆):

| Field | API (camelCase) | Meaning |
|---|---|---|
| Enable switch | `enabled` | Feature gate (same role as `ENTERPRISE_USER_MEMORY_ENABLED`) |
| Memory ID | `memoryId` | Enterprise SEMANTIC Memory pool id |
| TopN | `topN` | 1–20 |
| Timeout | `timeoutSeconds` | 0.5–120 seconds |

Save persists into `gateway_runtime_settings` and **hot-reloads in-process** (same path as
`retrievalScope` / `ragflowStatusWebhook`). No Gateway restart required.

Effective path: `fetch_user_memory_text` / `memory_config_ready` / client timeout and `top_n`
read `config.runtime_settings()`, not frozen boot-only env.

- `enabled=false` → Search/Write no-op
- `enabled=true` + empty `memoryId` → allowed to save; runtime no-op (warn once); Q&A continues
- Old DB rows without `userMemory` → backfill from env on load (no crash)

Env vars remain the boot/default source when the section is first created.
