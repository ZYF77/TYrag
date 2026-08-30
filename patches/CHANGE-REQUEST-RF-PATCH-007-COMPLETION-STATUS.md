# CHANGE REQUEST：RF-PATCH-007 completion 显式业务终态

## 原因

Gateway 目前在 RAGFlow 未显式给出业务状态时，靠「回答是否为空、有无 chunks、
是否命中拒答措辞」猜 `completed` / `no_reliable_evidence` / `failed`。启发式判断
会误判（正文含拒答短语但其实是资料清单回答、被 Gateway 改写过的正文等），也违反
「消息业务状态与 citations 证据数据相互独立」的既定原则。

业务终态必须由产生回答的 RAGFlow completion 链路显式输出，Gateway 只校验、映射
（英文值 → 中文文案）并持久化，禁止按回答文本改判。

## 最小上游修改

- `ragflow/api/db/services/dialog_service.py`
  - 新增纯函数 `_completion_status(answer)`：最终答案为空白或精确等于
    `STANDARD_ABSTAIN_ANSWER`（允许首尾空白）→ `no_reliable_evidence`，否则
    `completed`；不做任何正则/模糊措辞判断。
  - `_grounding_abstain_event()` payload 增加 `"status": "no_reliable_evidence"`
    （覆盖空知识 empty_response 命中、prompt-fit 单块装不下、流式最终答案为空）。
  - Guard 融合弃答（`_fuse_or_keep` fused 分支）产物为标准拒答文案，由精确相等
    判断覆盖，最终帧同样得到 `no_reliable_evidence`。
  - completion 可达的最终事件统一附加 `status`：
    - `async_chat_solo` 流式 final（grounding fused 帧）与非流式 payload；
    - `async_chat` 流式 final（注意：先按 decorate_answer 结果计算 status，
      再按既有行为对非 grounding 清空 answer）、非流式 payload、
      非 grounding empty_response 终帧；
    - `rag_agent` 流式 final 与非流式 payload。
- `ragflow/api/apps/restful_apis/chat_api.py`（`session_completion`）
  - 流式 except 的 `code: 500` 错误帧 `data` 增加 `"status": "failed"`；
  - JSON 异常仍走 `server_error_response`（HTTP 错误），不动。

不改变 `code/message/data` 既有字段语义；不改 `async_ask` 等非 completion 链路；
不改 attachment_observations 机制与 files/附件链路；不新增依赖。

## 上游落点与升级冲突

| 文件 / 函数 | 修改原因 | 预期冲突点 |
|---|---|---|
| `dialog_service.py`：`_grounding_abstain_event`、`_completion_status`（新增） | 弃答事件与终态判定 | 上游若调整弃答 payload 结构，重放 `status` 键即可 |
| `dialog_service.py`：`async_chat_solo` 流式/非流式终帧 | fused 帧 status | 上游若调整 `_fuse_or_keep` 产物结构，需保持终帧携带 status |
| `dialog_service.py`：`async_chat` 流式 final、非流式 payload、empty_response 终帧 | 主链路 status；流式 final 必须在清空 answer 之前计算 | 上游若改变「非 grounding 终帧 answer 清空」行为，需同步检查 status 计算顺序 |
| `dialog_service.py`：`rag_agent` 流式 final、非流式 payload | agentic 链路 status | 上游若调整 decorate_answer 返回结构，需保持终帧携带 status |
| `chat_api.py`：`session_completion.stream()` except 错误帧 | 异常终态 `failed` | 上游若重构错误帧结构，需保留 `data.status` |

## 测试

- `ragflow/test/unit_test/api/db/services/test_dialog_service_grounding.py`
  - 新增：`test_completion_status_is_exact_match_only`（精确相等/空白/近似措辞不误判）、
    `test_grounding_abstain_event_carries_no_reliable_evidence_status`、
    `test_grounding_solo_stream_final_carries_status`；
  - 扩展既有断言（纯增量）：guard 弃答/短答重试仍弃答/空知识/prompt-fit 拒答 →
    `no_reliable_evidence`；guard 通过/非 grounding 流式与非流式/solo 流式/agentic
    final → `completed`。
- `ragflow/test/testcases/test_http_api/test_session_management/test_session_sdk_routes_unit.py`
  - 新增 `test_session_completion_stream_error_frame_carries_failed_status`：
    `rag_agent` 抛异常时 `code: 500` 错误帧 `data.status == "failed"`，且流仍以
    `{"code": 0, "data": True}` 收尾。
  - 顺带修复该文件内 harness 的基线损坏（与 status 无关，均为既有债务）：
    `tenant_model_service` stub 缺 `get_composite_model_name_by_id` /
    `get_model_config_by_id` / `resolve_model_id`，`dialog_service` stub 缺
    `rag_agent`，既有 spy 缺 `save_session` 形参；只补 stub/签名，不改断言。

## 部署与回滚

- 纯增量字段，RAGFlow 可先于 Gateway 发布；Gateway 读取该字段的开关由 Gateway
  侧任务控制。
- 回滚：删除上述 `status` 赋值即可，无数据/配置迁移；回滚时须同时关闭 Gateway
  对 `status` 的读取。
