# Gateway conversation device scope (P0)

## Modes (implicit — no EAM conversation-type field)

| Create | Mode | Meaning |
|--------|------|---------|
| with `equipmentId` | **scoped** | Create-time device is recorded as **anchor**; no-id turns use **active** |
| without `equipmentId` | **open** | No-id turns stay **wide** (ACL-available docs); no false bind |

Mode is sticky for the conversation lifetime. Later extracts may change `active` / `conversationDevices` but do not flip open↔scoped.

## Conversation state

- `equipmentId` / `equipment_id` = **active** device (EAM primary field unchanged)
- `conversationDevices` = ordered union of devices seen (JSON column `conversation_devices`)
- `anchorEquipmentId` = create-time device when scoped (`anchor_equipment_id`); resists eviction
- Limit default **2**, env `ENTERPRISE_CONVERSATION_DEVICE_LIMIT`
- Eviction: FIFO among prefer **non-anchor & non-active** first; **anchor last**

## This-turn retrieval (`_resolve_turn_scope`)

| Question | `turn_devices` | Conversation mutation |
|----------|----------------|------------------------|
| Explicit single id **B** | `{B}` (FOCUS) | Add B to devices; `active=B` (old devices kept) |
| Explicit multi / compare / 「这两台」 | multi subset | Union into devices; active unchanged unless explicit list ends on a new primary |
| No id + **scoped** | `{active}` | none |
| No id + **open** | empty → **wide ACL** | none |
| Unknown id + equipment cue | empty (fail-closed) | none |

Identifier Guard / `doc_ids` follow `turn_devices` ∩ ACL. Product-model adjacency still ignored.

## Non-goals (out of scope)

Soft knowledge expansion, dual models, complex Agent, forced EAM `equipmentIds[]`.
