# CHANGE-REQUEST：M3-C transient attachment 501 error code

## 状态

Open。此请求不修改冻结契约，等待 Lead 统一审核后决定是否纳入下一版契约。

## 原因

`contracts/integration-openapi-v2.yaml` v2.0.0 将
`POST /conversations/{conversationId}/attachments` 固定为 `planned` 且要求
501。`contracts/error-codes.yaml` 没有可精确表达该状态的 501 error code。
现有 Web mock 已使用 `ATTACHMENT_NOT_IMPLEMENTED`，因此 Gateway 在实验开关
关闭时使用同一 code 返回标准 error envelope，但不把该 code 静默加入冻结错误码表。

## 最小提案

下一版错误码基线新增：

```yaml
- code: ATTACHMENT_NOT_IMPLEMENTED
  http_status: 501
  retryable: false
```

消息建议为 `Transient attachment is planned but not enabled`。在该提案获批
前，运行时 501 仅是兼容性占位，不能作为公共 v2 attachment 已实现的声明。

## 替代方案与选择

- 使用 `INTERNAL_ERROR` 会丢失“planned/501”语义，且错误码与 HTTP 状态不精确匹配。
- 返回无 code 的 501 会违反项目标准 error envelope。
- 修改当前 OpenAPI 或错误码基线会越过 Lead 对冻结契约的所有权。

因此本提交只实现运行时安全 gate，并保留本 change request，不修改
`contracts/integration-openapi-v2.yaml`、`contracts/error-codes.yaml` 或公共 API shape。

## 兼容风险

客户端若未实现 `ATTACHMENT_NOT_IMPLEMENTED`，应仍以 HTTP 501 和标准 envelope
处理未知 code。若后续拒绝该 code 提案，必须在客户端与 Gateway 同步选择新的
冻结 code；不能仅修改服务端字符串。

## 回滚

Lead 可在契约审核后关闭或改写本请求；运行时回滚只需关闭
`ENTERPRISE_TRANSIENT_ATTACHMENTS_ENABLED`（默认即为 false），不会影响既有
conversation、query/SSE、FILE_SHARE ticket 或对象存储适配。
