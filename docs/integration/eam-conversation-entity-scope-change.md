# EAM 会话设备 Scope 变更说明

## 变更结论

- API 路径、字段、状态、SSE 事件和 `reasoningMode` 不变。
- Conversation 的 `equipmentId/fixedAssetNo` 改为当前活动设备，不再永久锁定整个会话。
- EAM 页面切换设备时继续调用现有 `PATCH /enterprise/api/v2/conversations/{id}/context`，成功后再发送消息。
- 用户在问题中写出一个完整设备号时，Gateway 在同租户可用文档中唯一匹配后切换本轮 Scope；写出多个设备号时，本轮可以比较这些设备。

## EAM 需要做什么

1. 创建会话时仍可提交初始 `equipmentId/fixedAssetNo`。
2. 页面主动切换设备时先 PATCH context，并刷新使用新 `contextVersion` 的 suggestions。
3. 消息请求仍只传原有 `question` 或 `suggestionId/contextVersion`，不要新增设备字段。
4. 同一 `clientMessageId` 重试会回放首次运行结果，不会按后来切换的设备重新检索。

## 联调阶段权限

- JWT 和接口 capability 仍必需，tenant 隔离始终生效。
- 文档的用户组、安全等级和部门权限暂不判断；同租户 active/current、质量通过的文档默认可访问。
- 有设备 Scope 时只检索该设备对应的可用文档；明确但不存在或不可检索的设备不会回退到全库。

## 兼容性

现有 EAM 客户端无需增加字段。原来“一台设备新建一个 Conversation”的调用方式仍可继续使用；需要同会话切换或比较时才使用新语义。
