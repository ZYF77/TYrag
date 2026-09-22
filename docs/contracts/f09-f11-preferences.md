# F09/F11 增量契约（2026-09-21）

既有消息状态、正文和 citations 相互独立，JSON/SSE 字段不变。reasoning / reasoning.delta 现在只含固定安全过程提示；未标记旧推理不展示。Workflow 幂等回放遵守 Accept: text/event-stream；失败 JSON 非 2xx，失败 SSE 只发 run.failed，安全部分结果可查历史。

以下接口位于 `/enterprise/api/v1/ai/memory`，使用既有 Token 与 ask capability，拒绝客户端主体覆盖，JSON body 禁止额外字段。

| 方法/路径 | 请求 | 响应 |
|---|---|---|
| GET `/preferences` | 可选 conversation_id，仅筛本人的候选 | enabled、preferences[{key,value,revision}]、candidates[{id,key,value,revision,conversation_id}] |
| POST `/candidates/{id}` | {revision: 非负整数, confirm: boolean} | {status: confirmed 或 ignored}；重复同决策幂等 |
| DELETE `/preferences/{key}` | {revision: 非负整数} | {key,value:null,revision:新修订} |

有限取值：language: zh/en；detail: brief/detailed；format: paragraphs/list。候选 7 天过期。404 PREFERENCE_NOT_FOUND；409 PREFERENCE_REVISION_CONFLICT/INVALID_PREFERENCE；503 MEMORY_NOT_CONFIGURED。失败不改变旧确认值或候选状态。

无额外 SSE 事件。前端收到终态后查候选；确认不是再次提交问答。旧 `/me` 查看/遗忘接口保留，但其自由文本记录不再注入回答。
