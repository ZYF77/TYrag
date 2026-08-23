# ADR-010 EAM v2 拆除 Grounding Mega Switch

- 状态：Accepted
- 日期：2026-08-20
- 取代：[ADR-009](ADR-009-EAM-v2-最小-Grounding-Guard.md)

## 决策

`grounding_version=1` 不再同时控制脱敏、候选 token 缓冲和简单/档位路径。拆成三个独立旋钮：

| 旋钮 | 由谁决定 | 当前默认 |
|---|---|---|
| Prompt 标记 / 日志脱敏 / empty_response / Langfuse 抑制 | Gateway 仍传 `grounding_version=1` | 开 |
| 候选 token 缓冲 | `_IDENTIFIER_NUMERIC_FUSE_ENABLED` | 关（Fuse 关则真流式） |
| 简单 chat vs low/medium/high/ultra | 请求 `reasoningMode` → RAGFlow `reasoning` 1–4；`simple` 不传该键 | `simple` |

Identifier Fuse、Numeric Fuse、短答重试、双模型校验保持关闭。只有 Eval 证明仍在编设备号/工单号时，才单独重开 Identifier Guard。Numeric Guard 不恢复。

Gateway SSE 边收边发 `reasoning.delta` / `answer.delta`。终态改写（引用清洗、拒答、资料清单救援、设备号提示）若与已流出正文不同，在 `citation` 前发 `answer.replaced`。回放只存合并单帧，不发 `answer.replaced`。

`high` / `ultra` 的 `web_search` 仍受 `internetEnabled` 与聊天联网配置双重约束。档位路径继续遵守 `doc_ids` 硬筛。Agentic 进度日志在 `grounding_version=1` 下只外送阶段标签，不外送问题原文或知识正文。

## 后果

- EAM 可继续省略 `reasoningMode`；已接 SSE 的客户端必须处理 `answer.replaced`。
- 真流式会先露出未清洗的正文，随后可能被整段替换。
- 推理档位提升理解与综合，不能替代 Identifier Guard。
