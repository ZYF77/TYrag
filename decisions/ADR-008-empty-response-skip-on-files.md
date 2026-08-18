# ADR-008 有附件时跳过 empty_response

- 状态：Accepted
- 日期：2026-08-18

## 决策

在 `dialog_service.async_chat` 对 `empty_response` 提前 return 增加 `not messages[-1].get("files")`。不修改共享 Chat `prompt_config`。

## 原因

公开 API 无法按请求关闭 `empty_response`；PATCH 共享 Chat 会换 dialog 或污染无附件路径。问询附件混合模式需要原件进入最终生成。

## 后果

有附件且检索为空时继续调用 LLM。无附件行为不变。升级时按 `patches/CHANGE-REQUEST-EMPTY-RESPONSE-SKIP-ON-FILES.md` 重放或删除。
