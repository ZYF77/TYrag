# 状态机契约

## 1. 同步事件

```text
received
→ validating
→ accepted
→ transferring/registering
→ tracking
→ completed

任意可恢复阶段 → retry_wait → 原阶段
不可恢复 → failed
重复事件 → deduplicated
```

## 2. 文档知识版本

```text
registered
→ queued
→ parsing
→ indexing
→ validating
→ ready

质量问题 → review_required → retry/accepted/rejected
新版本 ready → 旧版本 superseded
业务停用 → disabled
逻辑删除 → deleted
```

只有 `ready + business_status=active + current_version=true` 可以进入普通用户检索。

## 3. 会话运行

```text
started
→ authorizing
→ routing
→ retrieving/querying
→ answering
→ completed

客户端断开 → client_disconnected
模型/依赖失败 → failed 或 degraded_completed
无证据 → completed(no_reliable_evidence)
```

## 4. 状态更新规则

- 状态只能由服务端推进；
- 必须记录 `updated_at`、`request_id` 和触发者；
- 终态不可被旧任务覆盖；
- 重试创建新的 run/attempt，不覆盖历史错误；
- 前端不根据时间自行推断状态。
