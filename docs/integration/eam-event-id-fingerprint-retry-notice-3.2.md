# EAM 设备内容指纹 eventId 与失败重投约定（Document Feed 3.2）

> 状态：Gateway 代码已补全；完成本机 HTTP/EAM 联调后生效
> 日期：2026-08-28
> 面向：EAM 开发 / 测试 / 运维
> 适用接口：`POST /enterprise/api/v3/documents`

## 1. 结论

EAM 可以继续使用“设备内容指纹”作为 `eventId`：

- 设备内容不变，`eventId` 保持不变；
- 网络超时重发、系统自动补偿和可重试失败后的再次投喂，都可以发送相同 `eventId` 和相同业务请求；
- EAM 不需要为了重新投喂而修改设备内容，也不需要生成一个假的新内容版本。

Gateway 将根据当前处理状态区分“重复请求”和“失败后重新处理”，不是看到相同 `eventId` 就一律返回上次失败。

## 2. eventId 的含义

本次约定中：

```text
eventId = EAM 生成的设备内容指纹
```

同一个 `eventId` 必须始终代表同一份业务内容和同一组身份信息，包括：

- `tenantId`、`sourceSystem`；
- `externalDocumentId`、`sourceVersionId`；
- `source.content`；
- `metadata.equipment_id` 以及其他 Metadata；
- `fileName`、`mediaType`、`source.kind`。

如果其中任意业务内容或身份信息发生变化，EAM 应生成新的 `sourceVersionId` 和新的内容指纹 `eventId`。不得在相同 `eventId` 下替换内容，否则 Gateway 返回：

```text
409 EVENT_ID_CONFLICT
```

HTTP 请求重发时可以重新序列化 JSON，但必须对最终发送的 raw body 重新计算现有 HMAC 签名。

## 3. 相同 eventId 再次投喂时的处理

| Gateway 当前状态 | 相同 eventId、相同业务请求再次到达 | 结果 |
|---|---|---|
| 尚未登记 | 创建处理任务 | 202，`deduplicated=false` |
| 正在上传、解析或索引 | 不创建第二个任务 | 202，`deduplicated=true` |
| 已成功并可检索 | 不重复上传、解析和生成向量 | 202，`deduplicated=true` |
| 上次失败且允许重试 | 重新启动处理 | 202，`deduplicated=false` |
| 上次失败但不允许重试 | 不重复执行无效处理 | 保留原失败结果 |
| 相同 eventId 但请求内容不同 | 拒绝覆盖原事件 | 409 `EVENT_ID_CONFLICT` |

失败后重新启动时，Gateway 内部自行判断：

- 尚未创建知识库文档：重新执行上传、解析和索引；
- 已创建知识库文档：复用现有文档重新解析和索引。

这些差异对 EAM 不可见，EAM 不需要判断知识库内部是否已经创建文档。

## 4. EAM 的重试规则

### 4.1 未收到 HTTP 响应

例如连接超时、连接中断，EAM 不确定 Gateway 是否已经受理：

- 保持相同 `eventId`；
- 保持相同业务请求；
- 使用新的 `X-TY-Timestamp` 和 `X-TY-Signature`；
- 按现有退避策略重新请求。

### 4.2 已收到 202

HTTP 202 只表示 Gateway 已受理或识别为幂等重放，不表示已经可以问答。

EAM 应等待 `document.terminal` 回调，不要因为短时间内没有完成就修改内容或更换 `eventId`。

### 4.3 收到可重试失败

终态回调示例：

```json
{
  "originatingEventId": "<设备内容指纹>",
  "status": "failed",
  "retrievable": false,
  "error": {
    "code": "RAGFLOW_UNAVAILABLE",
    "message": "文档处理服务暂时不可用，请稍后重试。",
    "retryable": true
  }
}
```

当 `error.retryable=true` 时，EAM 可以在退避后重新发送原投喂请求：

- `eventId` 不变；
- `sourceVersionId` 不变；
- `source.content` 和 Metadata 不变；
- 只更新本次 HTTP 请求的 HMAC timestamp/signature。

Gateway 接受重新处理时返回：

```json
{
  "operationId": "<设备内容指纹>",
  "externalDocumentId": "FAC-10086-MASTER",
  "sourceVersionId": "v1",
  "deduplicated": false,
  "updatedAt": "2026-08-28T02:00:00+00:00"
}
```

`deduplicated=false` 表示本次已重新启动处理；它不代表设备内容发生了变化。

### 4.4 收到不可重试失败

当 `error.retryable=false`，或同步返回 409/422 时，重复发送同一内容通常不能解决问题。

EAM 应先根据错误修正数据或版本：

- 内容、Metadata 或设备关联发生变化：生成新的 `sourceVersionId` 和新的内容指纹 `eventId`；
- `EVENT_ID_CONFLICT`：恢复原请求，或者为实际发生变化的内容生成新版本；
- `DOCUMENT_VERSION_CONFLICT`：内容变化时必须使用新的 `sourceVersionId`；
- 校验错误：修正请求后按新内容重新计算 `eventId`。

## 5. 终态回调幂等

重新处理完成后，Gateway 会产生该处理轮次对应的终态回调：

- `originatingEventId` 仍然是相同的设备内容指纹；
- 每个新的处理轮次使用新的 `deliveryId`；
- 同一个回调因网络失败而重发时，`deliveryId` 保持不变。

因此 EAM 必须：

```text
按 deliveryId 做回调幂等
不要按 originatingEventId 丢弃后续处理轮次的回调
```

例如同一个内容指纹可以先收到一次 `failed`，重新处理后再收到一次 `retrievable`。第二次回调是有效的新结果，不能因为 `originatingEventId` 相同而忽略。

## 6. EAM 不需要增加的内容

本约定不新增：

- 新的投喂 URL；
- `retryId`、`forceRetry` 等请求字段；
- RAGFlow 文档 ID；
- Parser、Chunk 或 Embedding 参数；
- EAM 侧知识库状态判断。

EAM 仍调用原接口，并根据 HTTP 结果与终态回调中的 `retryable` 决定是否重发。

## 7. 联调验收用例

双方至少验证：

1. 首次投喂返回 202，最终回调为 `retrievable`；
2. 成功后重复相同请求，返回 `deduplicated=true`，不重复生成向量；
3. 处理中重复相同请求，不启动第二个任务；
4. 模拟可重试失败后，以相同 `eventId` 重发，返回 `deduplicated=false` 并重新处理；
5. 失败发生在知识库文档创建前和创建后，两种情况都能恢复；
6. 同一内容指纹先回调 `failed`、后回调 `retrievable`，两个处理轮次的 `deliveryId` 不同；
7. 相同 `eventId` 修改 `source.content` 或 Metadata，返回 409；
8. `retryable=false` 的失败不会被无休止自动重试。

## 8. 生效条件

本文描述的失败重处理和回调轮次逻辑已在 Gateway 代码中实现。正式通知 EAM 上线前，还需要完成并验证：

1. 本机真实 HTTP E2E 覆盖本文件第 7 节用例；
2. EAM 联调确认按 `retryable` 和 `deliveryId` 执行客户端逻辑。

完成上述验证前，不应把本文件视为目标环境已部署完成的行为承诺。

相关协议：

- [`eam-json-feed-handoff-3.2.md`](./eam-json-feed-handoff-3.2.md)
- [`eam-feed-callback.md`](./eam-feed-callback.md)
- [`../../contracts/document-feed-v3.2.yaml`](../../contracts/document-feed-v3.2.yaml)
