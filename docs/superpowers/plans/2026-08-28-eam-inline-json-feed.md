# EAM 通用 JSON 投喂最小实施计划

> 状态：代码与 3.2 契约已实现；已验证的聚焦测试通过，新增重解析调用链测试待本机 PostgreSQL 恢复；本机真实 HTTP E2E 因运行配置缺失而阻塞，未部署
> 日期：2026-08-28
> 外部提案：Document Feed API 3.2.0
> 兼容基线：FILE_SHARE 3.1.0、终态回调 1.0.0

## 目标

EAM 继续调用：

```http
POST /enterprise/api/v3/documents
```

新增 `source.kind=INLINE_JSON`。`source.content` 接受任意 JSON object，每份 JSON 必须通过 `metadata.equipment_id` 关联一个设备。普通未知字段无需预先注册，上传后由 RAGFlow 内置 JSON Parser 分块、Embedding 和索引。

## 已确认的最小架构

```text
EAM INLINE_JSON
  → Gateway 校验固定 Envelope、equipment_id、敏感字段、大小和幂等
  → Gateway 稳定序列化 source.content，并计算内部 SHA-256
  → 以 .json 文件调用现有 RAGFlow upload_document
  → 设置既有企业 Metadata，并确保 chunk_method=naive
  → 调用现有 start_parsing
  → RAGFlow 内置 JSON Parser 分块、Embedding、索引
  → Gateway 验证 DONE 且至少存在一个非空 Chunk
  → 复用现有版本提升和 document.terminal 回调
```

采用该路线的当前代码依据：

- `ragflow/deepdoc/parser/json_parser.py` 已支持 JSON/JSONL 结构拆分；
- `ragflow/rag/app/naive.py` 已按 `.json` 后缀调用 `JsonParser`；
- `enterprise/gateway/sync/ragflow_document_client.py` 已有上传、设置 Metadata、启动解析和 Chunk 读回能力；
- `enterprise/gateway/sync/sync_service.py` 已有文档上传、幂等、版本、状态和回调编排。

真实企业 Dataset 的 Parser 配置及中文/英文混合字段召回效果仍需通过本机真实 HTTP E2E 验证；验证失败前不引入自定义分块器。

## 本期明确不做

- 不新增 `/facility`、`/repair`、`/maintenance` 等业务接口；
- 不自建 Gateway JSON 分块器；
- 不使用 RAGFlow Virtual Document + Add Chunk；
- 不建设 `facility:v1`、YAML Profile、模板或字段映射系统；
- 不建设数据库在线配置、管理页面、审批或热更新；
- 不保存 Chunk 到 JSONPath 的新映射表；
- 不增加字段级 JSONPath citation 或原文访问接口；
- 不修改 RAGFlow 上游；
- 不增加新的对象存储适配器；
- 不让 EAM 传 Parser、Chunk、Embedding、Profile 或内容 `sha256`。

只有真实 E2E 证明 RAGFlow 通用 JSON Parser 的召回质量不足，才单独评估服务端静态字段增强；该优化不属于首版完成条件。

## 开工说明

- **成功标准：** INLINE_JSON 首次登记返回 202；RAGFlow 解析完成且产生非空 Chunk；终态回调为 `retrievable`；新增普通字段可通过关键词或语义召回；设备 Scope、幂等和 FILE_SHARE 回归不变。
- **读取/修改范围：** 新增 3.2 OpenAPI；修改 `enterprise/gateway/sync/v3_router.py`、`enterprise/gateway/sync/sync_service.py`、`enterprise/gateway/sync/readiness.py`；新增聚焦测试；扩展既有 E2E runner；更新本计划和 EAM 3.2 说明。
- **契约版本：** 新增 Document Feed 3.2.0；`contracts/file-share-v3.yaml` 3.1.0 和 `contracts/file-share-callback-v1.yaml` 1.0.0 不改。
- **不会修改：** RAGFlow 上游、官方迁移、数据库表、根依赖锁、auth/ACL、查询 API、部署文件和 30 联调机。
- **主要风险：** `.json` 文档必须使用 `naive/general`；JSON Parser 的原始 key/value 文本可能需要真实召回验证；质量门不能继续强制 PDF 的 position 能力；当前工作区已有大量无关未提交修改，实施时必须定点修改。

## 2026-08-28 执行记录

已完成：

- 新增 `contracts/document-feed-v3.2.yaml`，保留 FILE_SHARE 3.1.0 与 callback 1.0.0；
- v3 Router 支持 `FILE_SHARE | INLINE_JSON`，INLINE_JSON 不接收业务 `sha256`；
- 实现 2 MiB 请求上限、20 层嵌套上限、敏感/附件字段名拒绝和 `equipment_id` 冲突校验；
- Router 与 worker 复用同一个稳定 JSON 序列化/内部 SHA-256 helper；
- INLINE_JSON 不调用对象存储，复用现有 RAGFlow upload、`chunk_method=naive`、parse 和 Chunk 读回链路；
- INLINE_JSON 质量声明仅要求 `text`，状态/质量/版本/回调继续复用原编排；
- v3 状态查询与 readiness 已覆盖 INLINE_JSON；
- E2E runner 已加入 INLINE_JSON 登记、幂等、冲突、安全负例、RAGFlow Chunk、查询引用、回调检查和可重试失败重投检查。

验证结果：

- `test_inline_json_feed_v3.py + test_file_share_v3_hmac.py`：9 passed；增加大小/readiness 用例后 INLINE_JSON 聚焦测试为 10 passed；
- `test_inline_json_feed_v3.py + test_sync_dataset_config.py`：12 passed；
- `test_enterprise_runner.py`：17 passed；
- 真实 runner：`BLOCKED / required_local_configuration_missing`，证据写入 `artifacts/e2e/file-share-v3-v2-inline-json-20260828/`，未产生真实业务验收结论；
- 计划内旧回归文件受工作区既有数据库适配迁移影响：`test_ragflow_delete_sync.py` 仍引用已移除的 `models.init_db`；callback/status 旧测试仍按二元组解包现已返回三元组的 `isolated_gateway_db`。这些失败发生在旧 fixture/收集阶段，不是 INLINE_JSON 断言失败，也尚未在本任务中扩展为整套数据库迁移修复。

## 协议决策

### 固定外层，灵活内容

INLINE_JSON 请求继续复用 v3 外层身份、版本、HMAC、Metadata 和回调语义。新增部分只有：

```json
{
  "fileName": "FAC-10086-MASTER.json",
  "mediaType": "application/json",
  "source": {
    "kind": "INLINE_JSON",
    "content": {
      "any_future_field": "无需修改接口代码"
    }
  }
}
```

`source.content` 必须是 object；内部允许标准 JSON scalar、object 和 array。

### equipment_id

- `metadata.equipment_id` 必填，是 Scope/ACL 和精确过滤的权威值；
- 第一阶段一份 JSON 只归属一个设备；
- `source.content` 可以不包含设备号；若包含明确的 `equipment_id` 且与 Metadata 冲突，返回 422；
- 其他普通字段只进入文本索引，不自动提升为 Metadata。

### 哈希与认证

- inbound HMAC 维持现有 v3.1 安全规则；HMAC 内部 raw-body hash 不是请求字段；
- INLINE_JSON 不接收 EAM 提供的内容 `sha256`；
- Gateway 使用 `sync_service.py` 内一个共享 helper 稳定序列化 `source.content` 并计算内部 SHA-256，Router 和异步同步流程复用同一实现，避免哈希与上传字节漂移；
- FILE_SHARE 的文件 `sha256` 要求保持不变。

### 最小安全限制

- 复用或增加一个固定请求总大小上限；
- 增加一个最大嵌套深度，防止恶意递归；
- 对字段名做大小写及 `_`/`-` 归一化，命中凭据或明显附件内容字段时整体返回 422；
- 不实现通用 Base64 猜测器，不静默删除字段后继续受理；
- 不增加叶子数量、数组长度、单字符串长度等多套独立限额，除非真实压测证明总大小和深度不足。

## 实施任务

### Task 1：冻结 Document Feed 3.2 契约

**Files**

- Create: `contracts/document-feed-v3.2.yaml`
- Modify: `enterprise/gateway/sync/v3_router.py`
- Modify: `enterprise/gateway/sync/sync_service.py`（共享稳定序列化 helper）
- Create: `enterprise/tests/test_inline_json_feed_v3.py`

**实现**

- 以 `source.kind` 区分 `FILE_SHARE` 和 `INLINE_JSON`；
- FILE_SHARE 继续要求 `sha256`、`application/pdf` 和原 source 字段；
- INLINE_JSON 不要求也不接受业务内容 `sha256`，要求 `fileName` 以 `.json` 结尾、`mediaType=application/json`、`source.content` 为 object；
- 保留现有 Metadata schema，明确 `metadata.equipment_id` 必填；
- 调用共享 helper 生成稳定 JSON bytes 和 SHA-256；
- 保留 HMAC、credential binding、事件回执和版本冲突语义；
- 增加请求总大小、深度、敏感/附件字段名负例。

**验证**

```powershell
python -m pytest enterprise/tests/test_inline_json_feed_v3.py enterprise/tests/test_file_share_v3_hmac.py -q --basetemp=C:\CodingProgram\WAES\TYrag\.pytest-tmp-json-v3
```

### Task 2：复用现有 RAGFlow 上传解析链路

**Files**

- Modify: `enterprise/gateway/sync/sync_service.py`
- Modify: `enterprise/gateway/sync/readiness.py`
- Test: `enterprise/tests/test_inline_json_feed_v3.py`

**实现**

- INLINE_JSON 不调用 S3/FILE_SHARE source adapter；
- 从异步事件中的 `source.content` 调用同一共享 helper 重新生成稳定 JSON bytes，校验其哈希与映射一致后构造成现有 `SourceFile`；
- 复用 `upload_document`，不新增 RAGFlow Client 方法；
- 在解析开始前写入既有企业 Metadata，并令该文档 `chunk_method=naive`；
- 复用 `start_parsing`、Document 读回、Chunk 读回、重试和删除逻辑；
- 完成条件为 RAGFlow `DONE` 且至少一个 Chunk 的 `content` 非空，不预测精确 Chunk 数量。

**验证**

```powershell
python -m pytest enterprise/tests/test_inline_json_feed_v3.py enterprise/tests/test_sync_dataset_config.py enterprise/tests/test_ragflow_delete_sync.py -q --basetemp=C:\CodingProgram\WAES\TYrag\.pytest-tmp-json-sync
```

### Task 3：收敛质量门、版本提升和回调

**Files**

- Modify: `enterprise/gateway/sync/sync_service.py`
- Test: `enterprise/tests/test_inline_json_feed_v3.py`
- Test: `enterprise/tests/test_callback_delivery.py`

**实现**

- INLINE_JSON 的质量声明只要求非空文本，不要求 PDF position/table/image 能力；
- 继续走既有质量/版本提升编排，避免新增第二套终态状态机；
- 只有当前版本、active、解析完成、非空 Chunk、质量允许时才 `retrievable`；
- 复用现有 `document.terminal` body、outbound HMAC、`deliveryId` 幂等和重试；
- 回调不新增 JSON 专用字段或 URL。

**验证**

```powershell
python -m pytest enterprise/tests/test_inline_json_feed_v3.py enterprise/tests/test_callback_delivery.py enterprise/tests/test_file_share_v3_status.py -q --basetemp=C:\CodingProgram\WAES\TYrag\.pytest-tmp-json-callback
```

### Task 4：真实 HTTP E2E 与 FILE_SHARE 回归

**Files**

- Modify: `enterprise/scripts/run_file_share_v3_v2_e2e.py`
- Modify: `docs/integration/eam-json-feed-handoff-3.2.md`

**实现/验收**

1. INLINE_JSON 首次登记 202，终态回调 `retrievable`；
2. 增加未声明字段后仍能登记，字段可通过关键词或语义召回且不成为 Metadata filter；
3. 相同事件重放不重复生成向量；同版本改内容返回 409；
4. 缺少/冲突 equipment_id、敏感字段、超限和跨设备 Scope 失败关闭；
5. 既有 FILE_SHARE 3.1 上传、解析、引用和回调回归通过。

**验证**

```powershell
python enterprise/scripts/run_file_share_v3_v2_e2e.py
```

必须记录本机真实请求、终态回调、RAGFlow Document/Chunk 读回和查询召回证据。离线单测、Stub 或 dry-run 不能替代该结论。

## 稳定 eventId 失败重处理补全

- `ext_document_map` 保存内部 `processing_round` 和 `last_error_retryable`；失败重投由数据库条件更新原子认领，并同步重置原 Outbox。
- Outbox 完成、重试和失败写回带处理轮次与 worker 锁定条件，旧 worker 不能覆盖新轮次。
- 无 RAGFlow 文档时重新上传；已有 RAGFlow 文档时复用文档重新解析，不新增 EAM 请求字段。
- 质量评估版本跟随处理轮次；回调唯一性包含处理轮次，同一轮复用 `deliveryId`、新轮次生成新 `deliveryId`。
- 不可重试失败保持去重；公共 URL、请求/回调 Body、HMAC、ACL 和 RAGFlow 上游不变。
- 已增加失败重处理、并发认领、质量轮次和回调轮次测试；真实 HTTP/EAM 联调仍需在依赖配置可用后完成。

## 2026-08-30 中断后续做记录

- 收窄新处理轮次的强制重新解析条件：只在本轮仍为 `UNSTART` 时调用 `start_parsing`，同轮自动重试不重复启动解析；
- v1→v2 升级补齐 `sync_outbox.processing_round`，并保持幂等；
- 新增“已有 RAGFlow 文档 ID、同一 `eventId` 失败重投后复用并重新解析”的调用链测试；当前本机 PostgreSQL `127.0.0.1:55432` 未启动，测试待数据库恢复后执行；
- 已验证：E2E runner 静态测试 `17 passed`、HMAC 契约测试 `1 passed`、模块导入/编译/测试收集通过；此前数据库可用时的聚焦实现测试和迁移测试结果保持有效；
- 本机真实 runner 当前仍返回 `BLOCKED / required_local_configuration_missing`，不形成真实 EAM/RAGFlow 验收结论。

## 完成门槛

- 新 3.2 OpenAPI 与实现一致；
- 聚焦单元、契约、HMAC、ACL 负例和 FILE_SHARE 回归通过；
- 本机真实 HTTP E2E 同一轮全部通过；
- 不新增 Gateway JSON 分块、Profile、JSONPath 表或 RAGFlow 上游补丁；
- 未部署 30 联调机，除非用户另行明确授权。
