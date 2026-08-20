# CHANGE REQUEST：EAM v2 Grounding 上下文

## 原因

RAGFlow 的引用结果可能包含未进入最终模型 prompt 的 chunk，不能作为 Final Guard 的
可靠证据。Gateway 跨服务索要 `effectiveKnowledge` 并把 Guard 放在 Session 之后，
会把协议故障放大成 `Message run failed` / 系统性拒答，且候选回答可能先写入
RAGFlow Session。

Final Guard 必须在 RAGFlow 持久化前执行；`effectiveKnowledge` 不再跨服务。

## 最小上游修改

- 仅当内部请求携带 `grounding_version=1` 时，在最终 prompt 中标记知识段，并在
  token 截断完成后提取模型实际看到的知识（内部使用，不对外返回）。
- 若 grounding prompt-fit 截掉知识 START marker，则仅从知识尾部逐块裁剪并重试；
  单块仍无法保留 marker 时不调用模型，走标准无依据终态。
- `decorate_answer` 之后、Session 持久化之前运行 Identifier/Numeric 保险丝。
  PASS 原样持久化；FAIL 且仅为数字问题（无标识符失败）且仍有 `effectiveKnowledge` 时，
  同批证据内自动短答重试一次（禁止新增数字/序号、禁止因此整句拒答，`max_tokens≤512`），
  再过同一保险丝；仍 FAIL 或无证据改为 `未找到可靠依据，无法回答。`，清空 chunks。
  列表序号（含行内 `1. / 2) / 3、`）不作为 numeric claim；`1.5 MPa` 等小数仍严格核对。
  检索元数量（找到 N 条片段、共 N 类/份）不作为 numeric claim；真实业务数量仍严格核对。
  若失败数字全是 `0/0.0+单位` 占位读数，可确定性剥离后再 fuse。
  **联调现状（2026-08-20）：** `dialog_service._IDENTIFIER_NUMERIC_FUSE_ENABLED=False`，
  实现保留但不调用；重新启用时改回 `True` 即可。
- grounding 请求禁止 yield candidate token；终态 payload 不再包含 `grounding` /
  `effectiveKnowledge`。
- 该内部请求不把完整问题、prompt、知识文本或模型输出写入日志和 tracing。
- 未携带版本的 RAGFlow 调用保持原行为。
- `dialog_service.py` 不引入 `equipmentId` 或 EAM 状态枚举。

不新增依赖、公开配置、数据库字段、`visibleChunkIds` 或另一套生成接口。

## 上游落点与升级冲突

| 文件 / 函数 | 修改原因 | 预期冲突点 |
|---|---|---|
| `ragflow/rag/grounding/guard.py`、`__init__.py` | Identifier/Numeric 保险丝纯函数；规则与原先 Gateway `grounding.py` 一致 | 新模块，升级时原样重放 |
| `ragflow/api/db/services/dialog_service.py`：`async_chat`、`async_chat_solo`、`rag_agent`、`get_models` | prompt 标记、尾部裁剪、持久化前 fuse、grounding 请求跳过 SQL/agentic reasoning/TTS 与正文日志 | 上游 prompt 组装、`message_fit_in`、流式终态结构调整 |
| `ragflow/api/db/services/tenant_llm_service.py`：`LLM4Tenant.__init__` | 按请求禁止 Langfuse | 上游 tracing 初始化调整 |
| `ragflow/api/db/services/llm_service.py`：`LLMBundle.clone` | 克隆模型时保持禁止 tracing | 上游 clone 参数调整 |
| `ragflow/rag/llm/chat_model.py`：Base/LiteLLM chat history logging | grounding 请求只记录消息数量，不记录 prompt/history 正文 | 上游模型请求日志调整 |
| `ragflow/rag/prompts/generator.py`：`cross_languages` | 复用已禁正文日志的当前 chat model | 上游 query refinement 签名调整 |
| `ragflow/conf/service_conf.yaml`、`ragflow/docker/service_conf.yaml.template` | `minio.bucket` 必须为 `ragflow`；Compose 嵌套 YAML 不会被 `MINIO_BUCKET` 单独覆盖 | 上游默认 bucket / 模板变量名 |

## MinIO 漂移对象回拷

2026-08-19 14:09 之后，空 `minio.bucket` 会把对象写到「知识库 ID」同名 bucket，而不是
`ragflow/<dataset_id>/<object>`。引用图因此打不开。配置已改回 `ragflow`；已漂移对象
需复制，不要先删源。

本机 / 联调机（凭证只从 Compose/`MINIO_*` 环境读取，禁止写进仓库或日志）：

```text
# 1. 确认当前配置
grep -n "bucket:" ragflow/conf/service_conf.yaml ragflow/docker/service_conf.yaml.template

# 2. 列出非 ragflow 的知识库 ID bucket（长度像 kb id 的 bucket 名）
mc alias set local http://127.0.0.1:9000 "$MINIO_USER" "$MINIO_PASSWORD"
mc ls local

# 3. 把 14:09 之后的对象复制回 ragflow/<bucket>/<object>
#    对每个漂移 bucket $KB：
mc cp --recursive "local/$KB/" "local/ragflow/$KB/"

# 4. 抽样：原路径 $KB/$object 与 ragflow/$KB/$object 都存在后再考虑删除源 bucket
```

未执行回拷时，旧引用图仍 404 或 JSON `code:102`。Gateway 把 HTTP 200 + JSON
`{"code":102}` 当缺失，不得当二进制转发。

本机 2026-08-19 已执行回拷：1 个知识库 ID bucket、25 个对象写入 `ragflow/<dataset_id>/`。
这些对象的 `last_modified` 均早于 14:09，但仍在错误 bucket 中，故全部复制、未删源。
30 联调机需按同样步骤盘点后再 recreate。

## 替代方案

- 使用 `reference.chunks`：可能包含模型未看到的文本，拒绝。
- 把 Guard 留在 Gateway 并跨服务传递 `effectiveKnowledge`：协议脆弱且候选可能先落
  Session，拒绝。
- 保留 RAGFlow session 后失败再删除：存在删除失败和并发污染窗口，拒绝。
- 继续投影 Gateway `messages` 并禁用 RAGFlow Session：多轮历史与 UI/Session 工具
  分叉，拒绝。

正确做法：Final Guard 在 RAGFlow 持久化前；`effectiveKnowledge` 不跨服务；v2 恢复
Session 创建/复用。

## 兼容与回滚

内部请求字段 `grounding_version` / `allowed_identifiers` / `attachment_observations`
为 Gateway→RAGFlow additive；直接 UI 和普通 API 不受影响。EAM OpenAPI wire schema
不变。升级时重放 `rag/grounding`、`dialog_service` 中受 `grounding_version=1` 限定的
prompt 标记、fuse 分支和无正文 logging/tracing。回滚时同时停止 Gateway 发送
`grounding_version=1`，并视需要把 `minio.bucket` 与对象布局一并回滚。
