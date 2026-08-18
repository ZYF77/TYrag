# CHANGE-REQUEST：有附件时跳过 empty_response

## 状态

Open。最小上游补丁，不 PATCH 共享 Chat `enterprise-formal-{tenant}`。

## 文件

`ragflow/api/db/services/dialog_service.py`

## 函数

`async_chat`（约 790 行，`knowledges` 计算之后、进入 LLM 之前）

## 原因

企业问询把原件放进 RAGFlow 最终 `chat_completion` / SSE 的 `files[]`。共享 Chat 的 `prompt_config.empty_response` 在 KB=0 时会提前 return，看图/看附件题永远走拒答。不能每轮 `update_chat(empty_response="")`：那会改共享 Chat，且 sibling Chat 会换 `dialog_id`、续问丢失 `ragflow_session_id`。

公开 API 无法按请求豁免该提前 return，因此做一行条件补丁：仅当 `messages[-1].files` 非空时继续进 LLM。无附件纯文字路径完全不变。

## 替代方案

- 动态 PATCH 共享 Chat：禁止（dialog_id / session 绑定）。
- 为附件另建 sibling Chat：续问丢 `ragflow_session_id`。
- Gateway 自己做多模态生成：超出外围层，重复 RAGFlow 能力。

## 冲突点

上游 `async_chat` 在 retrieval 之后、`system_content` 组装之前的 `empty_response` 提前 return。升级 v0.26.4+ 时若该 `if` 被重排、拆到 sync 路径或改用 kwargs，本补丁必须逐项重放。当前仅此一处 `empty_response` 提前 return。

## 兼容风险

- 有 `files` 且 KB=0 时会进 LLM；看图题可以 `已完成` + 空 `citations`。EAM 不得把「已完成」理解为「有知识库依据」。
- 无附件请求行为与现网一致。
- 不改官方迁移、全局状态枚举、主 OpenAPI、根锁文件。

## 回滚

恢复为：

```python
if not knowledges and prompt_config.get("empty_response"):
```

并删除本 CR、ADR-008 与 `patches/manifest.yaml` 中 RF-PATCH-003。
