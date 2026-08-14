# EAM 设备管理系统综合对接文档

版本：`1.2`

适用接口：`FILE_SHARE API v3.1.0`、`FILE_SHARE callback v1.0.0`、`Query API v2.1.0`

本文是设备管理系统 EAM 的对外交付版本，合并了文件接入、HMAC 鉴权、终态回调、问答、引用和联调要求。当前第一阶段入库与问询均信任 EAM 提交的设备标识，不要求知识库系统调用 EAM 业务 GET，也不要求 EAM 新增 Asset Registry 服务。

## 1. 对接范围

### 1.1 第一阶段：文件外部联调

```text
EAM 文件服务器
    -> HMAC POST FILE_SHARE v3
    -> Gateway
    -> RAGFlow 解析、切分、索引和质量检查
    -> Gateway 终态回调 POST 到 EAM
```

第一阶段只需要实现：

- PDF 文件落盘到双方约定的文件共享，并确保 30 服务器可以只读挂载该共享目录；
- 计算 PDF SHA-256；
- FILE_SHARE v3 HMAC 签名；
- 文档登记（`202` 瘦身受理回执）；
- 提供终态回调接收 URL，并校验 Gateway outbound 签名。

第一阶段的职责边界如下：

- EAM 是设备、文档编号、版本和业务元数据的来源方；
- 知识库系统读取 EAM 已经写入的 PDF，负责解析、切分、索引和质量检查；
- `metadata.equipment_id`、`metadata.fixed_asset_no` 等字段由 EAM 提供，第一阶段只做格式和请求内一致性校验；
- FILE_SHARE v3 入库不调用资产服务，也不以 EAM 资产 GET 可用作为入库前置条件；
- 如果未来问答或权限场景需要资产主数据解析，另行定义接口和验收范围，不影响本阶段文件入库。

### 1.2 第二阶段：用户问答联调（可选）

```text
EAM 用户 JWT
    -> 创建设备问答会话
    -> 提交问题或续问
    -> answer + citations
    -> 查询引用详情或原始 PDF
```

问答接口使用用户 JWT，不使用 HMAC secret，也不使用 RAGFlow API key。

第二阶段不是第一阶段文件入库的前置条件。未启用问答时，EAM 不需要提供 JWT、JWKS 或用户映射接口。

### 1.3 EAM 与知识库系统之间的接口清单

|接口或能力|提供方|第一阶段是否需要|说明|
|---|---|---:|---|
|文件共享目录|EAM/双方基础设施|是|EAM 写入 PDF；30 服务器挂载后由 Gateway 只读访问|
|`POST /enterprise/api/v3/documents`|知识库系统|是|EAM 使用 HMAC 登记文件坐标、摘要和元数据|
|终态回调接收 URL|EAM|是|Gateway 推送 `retrievable` / `failed` / `review_required`|
|GET status（诊断）|知识库系统|否|Console/运维诊断保留；不是 EAM 正式主路径|
|EAM 资产解析 GET|EAM|否|不属于第一阶段文件入库接口；未来如有独立需求再定义|
|JWT/JWKS|EAM|否|仅在启用第二阶段用户问答时需要|

第一阶段没有“知识库系统调用 EAM 业务接口”的要求（除终态回调由 Gateway 主动推送外）。EAM 提供可挂载的文件共享、调用 FILE_SHARE v3 登记接口，并提供回调接收端点。

如果未来因问答或权限需求启用 EAM 资产解析 GET，当前单租户方案不要求该接口增加 `tenantId` 查询参数；该接口与第一阶段文件入库分开定义，不改变 FILE_SHARE v3 中必须提交的固定 `tenantId=wp04e2e`。

## 2. 当前测试环境参数

以下是当前测试服务器的参数，正式环境由知识库系统重新交付：

| 参数 | 测试值 | 说明 |
|---|---|---|
| Gateway 地址 | `http://192.168.30.30:5188` | EAM 在其他主机时使用该地址；`127.0.0.1` 只表示本机 |
| `keyId` | `device-local-key` | 放入 `X-TY-Key-Id` |
| `tenantId` | `wp04e2e` | 顶层 body、metadata 和 HMAC binding 必须一致 |
| `sourceSystem` | `EAM` | 当前固定值 |
| `storageRootId` | `device-share` | FILE_SHARE 逻辑根目录标识 |
| HMAC secret | 单独安全交付 | 不写入本文、代码、URL、日志或 Git |
| Dataset | `设备问询知识库` | Gateway 内部使用，EAM 不直接访问 RAGFlow |

当前测试环境的 Dataset 和模型由 `admin@ragflow.io` 配置。EAM 不需要 Dataset ID、RAGFlow API key 或 RAGFlow 管理员密码。

当前项目只开通一个知识库租户，EAM 本身不实现多租户。`wp04e2e` 是 Gateway/RAGFlow 的固定技术租户标识，用于数据隔离和 HMAC credential 绑定，不代表 EAM 内部存在多个租户。EAM 的 FILE_SHARE v3 producer 固定使用该值即可，不需要在业务界面或业务规则中实现租户选择。

### 2.1 测试与生产的区别

| 项目 | 当前测试 | 正式环境 |
|---|---|---|
| Gateway | HTTP 内网地址 | HTTPS Gateway 地址 |
| 文件认证 | 测试 HMAC credential | EAM 专用 HMAC credential |
| 用户 JWT | 测试 HS256 机制 | EAM 使用 RS256 + JWKS |
| 资产主数据解析 | FILE_SHARE v3 入库不依赖 | 不属于本阶段，另行定义 |
| PDF | 非敏感测试 PDF | EAM 真实业务 PDF |

## 3. 系统边界

### 3.1 30/31 服务器职责

```text
31 EAM 应用服务器
    -> 写入双方约定的文件共享目录
    -> 调用 30 Gateway 的 FILE_SHARE v3 接口

30 知识库服务器
    -> 只读挂载 31 的文件共享目录
    -> Gateway 读取 PDF
    -> RAGFlow 解析、切分、索引和质量检查
```

`relativePath` 是相对于 `storageRootId` 对应共享根目录的路径。它不是 31 服务器的绝对路径，也不是要求知识库人员人工登录 31 查找文件。只有当 30 已经挂载 31 的同一个共享根目录时，Gateway 才能自动读取该路径。

EAM 只访问 Gateway。EAM 不得直接访问：

- RAGFlow API 或 RAGFlow Web 管理端口；
- MySQL、Redis/Valkey、Elasticsearch；
- MinIO 或对象存储管理端口；
- Gateway 数据库；
- Gateway 内部 source-ticket 接口。

Gateway 不接收 EAM HTTP 请求中的 PDF 二进制，只保存文件坐标、版本、摘要和状态，并从只读共享目录读取原文。PDF 原文始终由双方约定的文件服务器保存。第一阶段不调用 EAM 资产、维修、审批或其他业务接口。

## 4. FILE_SHARE 文件存放规范

### 4.1 `storageRootId` 的含义

`storageRootId` 是 Gateway 侧配置的逻辑根目录别名，不是服务器绝对路径，也不是 URL。

当前测试值：

```text
storageRootId = device-share
```

EAM 只提交相对于该根目录的 `relativePath`。EAM 不需要知道 Gateway 容器内的实际挂载路径。

双方需要在联调前确认以下映射：

```text
31 EAM 文件共享根目录：/data/zk/EAM/attachments
    -> 30 主机挂载目录：/home/zkadmin/tyrag-data/file-share（示例）
    -> Gateway 容器内的只读目录
    -> storageRootId=device-share
```

30 主机挂载目录必须先由运维使用双方批准的 NFS/SMB 等方式挂载并验证；不能
直接把 31 的绝对路径填入 `ENTERPRISE_FILE_SHARE_HOST_ROOT`，除非该路径本身
已经存在于 30 主机。当前 Compose 将 30 主机目录绑定到容器
`/var/lib/tyrag/file-share:ro`。

EAM 不需要通过 SCP 或 Gateway API 上传 PDF。EAM 将文件写入共享目录后，直接提交相对路径即可；
`relativePath` 不包含 `/data/zk/EAM/attachments` 这一层根目录。

### 4.2 推荐相对路径格式

推荐 EAM 将 PDF 按以下格式保存：

```text
eam/<tenantId>/<equipmentId>/<externalDocumentId>/<sourceVersionId>/<fileName>
```

示例：

```text
eam/wp04e2e/EQ-001/DEVICE-MANUAL-001/v1/device-manual.pdf
```

请求中的字段对应：

```json
{
  "storageRootId": "device-share",
  "relativePath": "eam/wp04e2e/EQ-001/DEVICE-MANUAL-001/v1/device-manual.pdf",
  "fileName": "device-manual.pdf"
}
```

路径规则：

- 只能传相对路径，不能传 `C:\...`、`/var/...` 或 `\\server\share\...`；
- 使用 `/` 作为路径分隔符；
- 不能出现空路径段、`.`、`..` 或目录穿越；
- `fileName` 不能包含 `/` 或 `\\`；
- `tenantId`、`equipmentId`、`externalDocumentId`、`sourceVersionId` 建议只使用字母、数字、点、短横线和下划线；
- 如果业务 ID 中包含 `/` 或其他路径字符，EAM 应先转换为安全 slug，原始业务 ID仍放在 `externalDocumentId` 字段；
- 同一份 PDF 登记后不能覆盖原文件；内容变化必须写入新的 `sourceVersionId` 路径。

### 4.3 文件写入顺序

EAM 不应直接把尚未写完的文件作为最终文件提交。推荐流程：

1. 写入临时文件，例如：
   ```text
   eam/wp04e2e/EQ-001/DEVICE-MANUAL-001/v1/device-manual.pdf.uploading
   ```
2. 写完并关闭文件，确认文件大小和 SHA-256；
3. 原子重命名为最终路径：
   ```text
   eam/wp04e2e/EQ-001/DEVICE-MANUAL-001/v1/device-manual.pdf
   ```
4. 调用 Gateway 登记最终路径；
5. 登记后不要修改、覆盖或移动该 PDF。

SHA-256 是 PDF 原始字节的完整性指纹，不是加密算法。它不包含可还原的 PDF 内容，也不替代文件共享权限或 HMAC 鉴权；知识库系统会在读取文件后重新计算并比对该值。

## 5. HMAC 鉴权

### 5.1 凭据交付

知识库系统通过安全渠道向 EAM 提供：

- `keyId`；
- HMAC secret；
- `tenantId`；
- `sourceSystem`；
- Gateway 地址；
- `storageRootId`。

HMAC secret 不通过 Gateway API 获取。EAM 只在后端服务或 Secret Manager 中保存，不放入前端、浏览器、URL、请求 body、日志、Postman Collection 或 Git。

每个生产调用系统应使用独立的 `keyId` 和 secret。HMAC secret 与 JWT 私钥、RAGFlow API key 必须互相独立。

### 5.2 请求头

所有 FILE_SHARE v3 请求必须携带：

```http
X-TY-Timestamp: <10位Unix秒时间戳>
X-TY-Key-Id: <keyId>
X-TY-Signature: v1=<小写十六进制HMAC-SHA256>
```

服务器允许时间偏差为 ±300 秒。EAM 服务器应启用 NTP 或等效时间同步服务。

### 5.3 签名原文

签名原文固定为以下五行，最后一行后不追加换行：

```text
v1
<timestamp>
<大写HTTP method>
<规范化path及排序后的RFC3986 query>
<原始请求体bytes的SHA-256小写十六进制值>
```

计算方式：

```text
bodyHash = SHA256(rawBodyBytes).hexLower()
canonical = "v1\n" + timestamp + "\n" + method.toUpperCase()
          + "\n" + canonicalPathAndQuery + "\n" + bodyHash
signature = "v1=" + HMAC-SHA256(secret, UTF8(canonical)).hexLower()
```

签名要求：

- Query 参数按 RFC3986 编码；
- Query 按编码后的 key/value 排序；
- POST 签名使用最终发送的原始 UTF-8 body；
- GET 无 body 时，对空字节计算 SHA-256；
- `statusUrl` 必须使用服务端返回的完整 path 和 query，不能自行重拼；
- 每次重试都必须重新生成 timestamp 和 signature；
- 同一 `eventId` 可以用于业务幂等，但不能重复使用完全相同的 HMAC 签名；
- 同一签名请求在服务器侧 10 分钟内不能重放。

FILE_SHARE v3 不使用 `Authorization: Bearer`，也不使用 RAGFlow API key。

## 6. PDF 登记

### 6.1 接口

```http
POST {BASE_URL}/enterprise/api/v3/documents
Content-Type: application/json
Accept: application/json
X-TY-Timestamp: <timestamp>
X-TY-Key-Id: <keyId>
X-TY-Signature: v1=<signature>
```

### 6.2 请求示例

以下 EAM 请求示例不传 `source.etag`。EAM 当前只提交顶层 PDF `sha256`；除非双方
另行约定独立的文件版本标识，否则不要增加 `source.etag`，更不能把 `sha256`
重复填入该字段。

```json
{
  "eventId": "evt-device-manual-001",
  "eventType": "upsert",
  "tenantId": "wp04e2e",
  "sourceSystem": "EAM",
  "externalDocumentId": "DEVICE-MANUAL-001",
  "sourceVersionId": "v1",
  "sha256": "<PDF文件64位SHA-256>",
  "fileName": "device-manual.pdf",
  "mediaType": "application/pdf",
  "source": {
    "kind": "FILE_SHARE",
    "storageRootId": "device-share",
    "relativePath": "eam/wp04e2e/EQ-001/DEVICE-MANUAL-001/v1/device-manual.pdf",
    "size": 123456
  },
  "metadata": {
    "schema_version": 1,
    "tenant_id": "wp04e2e",
    "external_document_id": "DEVICE-MANUAL-001",
    "source_system": "EAM",
    "equipment_id": "EQ-001",
    "fixed_asset_no": "FA-001",
    "document_type": "PRODUCT_MANUAL",
    "document_version": "v1",
    "department_id": "maintenance",
    "security_level": 2,
    "allow_group_ids": ["maintenance"],
    "deny_group_ids": [],
    "business_status": "active",
    "page_count": 10
  }
}
```

### 6.3 字段要求

|字段|必填|要求|
|---|---|---|
|`eventId`|是|业务事件唯一编号，最长 128；重试同一业务事件时保持不变|
|`eventType`|是|`upsert` 或 `reindex`；首次登记使用 `upsert`|
|`tenantId`|是|使用知识库系统提供的租户值|
|`sourceSystem`|是|固定 `EAM`|
|`externalDocumentId`|是|EAM 文档永久 ID|
|`sourceVersionId`|是|文档版本；文件内容变化必须新建版本|
|`sha256`|是|实际 PDF 原始字节的 SHA-256，64 位十六进制|
|`fileName`|是|PDF 文件名，不包含目录分隔符|
|`mediaType`|是|固定 `application/pdf`|
|`source.kind`|是|固定 `FILE_SHARE`|
|`source.storageRootId`|是|当前测试为 `device-share`|
|`source.relativePath`|是|符合第 4 节规则的相对路径|
|`source.size`|否|建议提供实际文件字节数|
|`source.etag`|否|EAM 当前不传；不得填入 PDF SHA-256。只有双方另行约定独立版本标识时才传|
|`metadata.schema_version`|是|固定 `1`|
|`metadata.tenant_id`|是|必须等于顶层 `tenantId`|
|`metadata.external_document_id`|是|必须等于顶层 `externalDocumentId`|
|`metadata.source_system`|是|必须等于顶层 `sourceSystem`|
|`metadata.equipment_id`|是|由 EAM 提供的设备业务标识；第一阶段不调用 Asset Registry 解析|
|`metadata.fixed_asset_no`|否|由 EAM 提供的固定资产标识；是否必填由 EAM 业务规则决定|
|`metadata.document_type`|是|使用 EAM 侧配置的文档类型名称，服务端只做非空和长度校验|
|`metadata.document_version`|是|必须对应 `sourceVersionId`|
|`metadata.department_id`|是|文档所属部门|
|`metadata.security_level`|是|范围 `0～9`|
|`metadata.allow_group_ids`|否|允许访问的用户组；建议按业务权限填写|
|`metadata.deny_group_ids`|否|禁止访问的用户组|
|`metadata.business_status`|是|正常可用文档使用 `active`|
|`metadata.page_count`|否|建议提供 PDF 实际页数|

`fixed_asset_no` 在接口契约中是可选字段；如果 EAM 业务规定每份文档必须绑定固定资产，可以在业务校验中将其作为必填。

第一阶段知识库系统不负责执行 EAM 业务规则，也不要求 EAM 新增资产查询接口。EAM 应保证提交的设备和固定资产字段符合自身业务数据。

### 6.4 响应

首次接收成功返回 HTTP `202` 瘦身受理回执（FILE_SHARE 3.1.0，无 `statusUrl`）：

```json
{
  "operationId": "evt-device-manual-001",
  "externalDocumentId": "DEVICE-MANUAL-001",
  "sourceVersionId": "v1",
  "deduplicated": false,
  "updatedAt": "2026-08-13T02:00:00+00:00"
}
```

`202` 只表示任务已接收，不表示 PDF 已完成解析、索引或可以问答。

## 7. 终态回调（正式路径）与诊断状态

正式对接中，EAM 提供回调接收 URL，运维写入 Gateway `ENTERPRISE_CALLBACK_ENDPOINTS`。Gateway 在文档到达终态后主动 POST，契约见 `contracts/file-share-callback-v1.yaml`。回调 `status` 仅为：

```text
retrievable | failed | review_required
```

只有收到 `status=retrievable` 时，才认为文档可问答。

GET status 仍可用于 Console/运维诊断，但不是 EAM 正式主路径：

```http
GET {BASE_URL}/enterprise/api/v3/documents/{externalDocumentId}/status?tenantId=...&sourceSystem=...&sourceVersionId=...
X-TY-Timestamp: <新的timestamp>
X-TY-Key-Id: <keyId>
X-TY-Signature: v1=<新的signature>
```

诊断侧只有以下条件全部满足时，才认为文档可问答：

```text
retrievable == true
parseCompleted == true
indexCompleted == true
qualityStatus == "passed"
errorCode == null
```

不要只根据 HTTP `200`、`status=ready` 或 `pipelineStatus=DONE` 判断完成。

## 8. 文档任务列表

```http
GET {BASE_URL}/enterprise/api/v3/documents/sync-status?tenantId=wp04e2e&sourceSystem=EAM&limit=20
```

响应：

```json
{
  "items": [
    {
      "externalDocumentId": "DEVICE-MANUAL-001",
      "sourceVersionId": "v1",
      "status": "ready",
      "pipelineStatus": "DONE",
      "parseCompleted": true,
      "indexCompleted": true,
      "qualityStatus": "passed",
      "retrievable": true,
      "errorCode": null
    }
  ]
}
```

## 9. 问答 JWT

### 9.1 身份边界（HMAC / JWT / 运维账号）

|能力|凭证|谁签发|谁校验|
|---|---|---|---|
|文件登记 FILE_SHARE v3|HMAC（`keyId` + secret）|双方约定 secret；EAM 签名请求|Gateway|
|用户问答（创建会话/提问/历史/citation）|User JWT（RS256）|**EAM**|**Gateway**（公钥/JWKS）|
|RAGFlow 管理后台|`admin@ragflow.io` 等运维账号|知识库运维|RAGFlow；**不得**写入 EAM 业务 JWT|

HMAC secret、JWT 私钥、RAGFlow API key 必须互相独立，不可混用。

### 9.2 第一阶段说明

第一阶段文件登记不需要 JWT。

当前测试环境可显式开启 HS256 测试 JWT，不能作为正式 EAM 认证方案。测试 JWT secret 不写入本文，需由知识库系统单独提供。

### 9.3 正式环境方案（EAM 签发，Gateway 只验签）

正式环境由 **EAM 签发** RS256 JWT；Gateway **不持有、不配置 EAM 私钥**，只通过 EAM 提供的 JWKS 验证公钥。

当前联调环境已确认（详见 `docs/integration/eam-inquiry-handoff.md` §3）：

|配置|已确认值|
|---|---|
|`issuer`|`http://192.168.30.31:5105`|
|`audience`|`tyrag-gateway`|
|JWKS（主）|`http://192.168.30.31:5105/.well-known/jwks.json`|
|JWKS（备）|`http://192.168.30.31:5105/api/v1/ai/jwks.json`|
|算法 / `kid`|`RS256` / `eam-rs256-1`|
|有效期|约 15 分钟|
|`sub`|EAM 用户 id，长期不变|
|claim 字段名|Gateway 默认（无需 `JWT_CLAIM_MAP`）|

知识库 Gateway 侧对应配置（公钥侧，无私钥）：

|环境变量|说明|
|---|---|
|`JWT_JWKS_URL`|EAM JWKS|
|`JWT_ISSUER`|与 Token `iss` 一致|
|`JWT_AUDIENCE`|与 Token `aud` 一致|
|`JWT_ALLOWED_ALGS`|正式为 `RS256`（勿用会清空 `JWT_JWKS_URL` 的 test overlay）|
|`JWT_CLAIM_MAP`|仅当 EAM 字段名与默认不符时调整|
|`ext_user_map`|合法 JWT 首次访问 **JIT** 自动开通为 `active`；`disabled` 仍 403。**不要求** EAM 交开通名单|

ACL：JWT 的 `tenant` / `groups` / `security_level` 必须与投喂 metadata 对齐。`department` 不参与硬拒绝（用户所属部门可以与文档归属部门不一致）。问询范围由会话 `equipmentId` 约束。

JWT 示例（联调常用 ACL 取值）：

```json
{
  "iss": "http://192.168.30.31:5105",
  "aud": "tyrag-gateway",
  "sub": "<EAM用户id>",
  "iat": 1786492800,
  "exp": 1786493700,
  "tenant": "wp04e2e",
  "department": ["2"],
  "roles": ["end_user"],
  "groups": ["maintenance"],
  "security_level": 2
}
```

常用 claims：`sub`（主体）、可选 `business_user_id`、`tenant`、`roles`、`department`、`groups`、`security_level`。用户提问至少需要 `roles` 包含 `end_user`。租户、用户组和安全等级会参与文档 ACL 判断；部门仅作身份声明，不硬匹配投喂 `department_id`。

EAM 不应让普通用户自行修改 JWT claims，也不应把 `admin@ragflow.io` 作为正式业务用户。该账号仅用于当前 RAGFlow 管理和测试环境验证。

**明确不做**：EAM 不直接调用 RAGFlow、不传 datasetId、不覆盖 dataset system prompt、不把运维 admin 写进业务 JWT；不要求开通名单；不要求 validate-token 回调。

## 10. 创建问答会话与开场选项（chips）

推荐 UI 流程：

1. `POST /conversations` 创建会话（可带设备上下文）
2. 渲染响应中的 `suggestions`（开场 chips）
3. 用户点选后发 `SuggestionMessageRequest`，或自由输入发 `QuestionMessageRequest`
4. 同一 `conversationId` 续问；历史用 `GET .../messages`

```http
POST {BASE_URL}/enterprise/api/v2/conversations
Authorization: Bearer <EAM签发的JWT>
Content-Type: application/json
Accept: application/json
```

请求：

```json
{
  "equipmentId": "EQ-001",
  "fixedAssetNo": "FA-001",
  "faultCode": "FAULT-001"
}
```

创建会话时建议至少提供 `equipmentId`（提问前必需）；`fixedAssetNo` 可选。若同时提供，Gateway **原样保存**，不做跨系统一致性回查。设备身份由 EAM 负责；问询与 FILE_SHARE 入库均不调用 Asset Registry / EAM GET。

成功返回 HTTP `201`（契约 v2.2+ 含 `suggestions`）：

```json
{
  "conversationId": "conversation-001",
  "title": "New conversation",
  "status": "active",
  "equipmentId": "EQ-001",
  "fixedAssetNo": "FA-001",
  "faultCode": "FAULT-001",
  "contextVersion": 1,
  "context": {
    "equipmentId": "EQ-001",
    "fixedAssetNo": "FA-001",
    "faultCode": "FAULT-001",
    "contextVersion": 1,
    "registryVersion": null
  },
  "suggestions": [
    {
      "suggestionId": "inspect-fault",
      "label": "检查当前故障",
      "displayPrompt": "请根据可靠文档说明当前故障的检查步骤。",
      "contextVersion": 1,
      "expiresAt": null
    },
    {
      "suggestionId": "maintenance",
      "label": "查看维护要求",
      "displayPrompt": "请根据可靠文档说明该设备的维护要求。",
      "contextVersion": 1,
      "expiresAt": null
    }
  ],
  "contextCompacted": false
}
```

要点：

- 设备上下文字段直接来自 EAM 请求体；`registryVersion` / `assetId` 在问询路径通常为 `null`。
- `suggestions` 由 Gateway **服务端定义**；EAM 只展示 `label`，发消息时传 `suggestionId` + 匹配的 `contextVersion`，**禁止**客户端自定义 chip prompt。
- `GET/PATCH` 会话详情同样返回最新 `suggestions`。也可调用 `GET .../suggestions` 单独刷新。
- `PATCH .../context` 成功后 `contextVersion` 递增，旧 `suggestionId` 会返回 `409 SUGGESTION_STALE`。
- `contextCompacted` 表示 Gateway 是否已对该会话做过滚动摘要（排障用）；**不**返回摘要正文。压缩对 EAM 透明，完整原文仍在消息历史中。

## 11. 提问和续问

```http
POST {BASE_URL}/enterprise/api/v2/conversations/{conversationId}/messages
Authorization: Bearer <EAM签发的JWT>
Content-Type: application/json
Accept: application/json
```

自由提问：

```json
{
  "clientMessageId": "device-message-001",
  "question": "请说明该设备的维护步骤。"
}
```

点选开场选项：

```json
{
  "clientMessageId": "device-message-002",
  "suggestionId": "maintenance",
  "contextVersion": 1
}
```

同一问题重试时保持相同的 `clientMessageId`；新问题必须生成新的 ID。续问始终使用**同一个** `conversationId`；EAM **不**发送 RAGFlow `session_id`。

成功返回 HTTP `200`：

```json
{
  "conversationId": "conversation-001",
  "clientMessageId": "device-message-001",
  "runId": "run-001",
  "messageId": "assistant-message-001",
  "answer": "维护步骤如下……",
  "status": "completed",
  "citations": [
    {
      "citationId": "citation-001",
      "sourceType": "document",
      "title": "device-manual.pdf",
      "externalDocumentId": "DEVICE-MANUAL-001",
      "sourceVersionId": "v1",
      "pageNo": 1,
      "bbox": null,
      "assetId": "FA-001",
      "excerpt": "相关维护步骤原文……"
    }
  ],
  "replayed": false
}
```

`status` 取值：

|值|含义|
|---|---|
|`completed`|问答正常完成|
|`no_reliable_evidence`|没有找到足够可靠的证据|
|`failed`|问答执行失败|

`status` 与 `citations` 相互独立，EAM 不得根据引用数量自行改判业务状态。

长对话时 Gateway 可能自动滚动摘要并重建内部引擎会话；EAM 侧 `conversationId` 不变，也**无需**调用压缩 API。

如需流式回答，可以对同一地址使用：

```http
Accept: text/event-stream
```

不需要逐字显示时，优先使用 JSON 方式。

## 12. 会话和历史

### 12.1 会话列表

```http
GET {BASE_URL}/enterprise/api/v2/conversations?limit=20&cursor=<nextCursor>
Authorization: Bearer <EAM签发的JWT>
```

### 12.2 会话详情

```http
GET {BASE_URL}/enterprise/api/v2/conversations/{conversationId}
Authorization: Bearer <EAM签发的JWT>
```

详情含当前 `suggestions` 与 `contextCompacted`。列表项为摘要，不含 chips。

### 12.3 消息历史

```http
GET {BASE_URL}/enterprise/api/v2/conversations/{conversationId}/messages?limit=20&cursor=<nextCursor>
Authorization: Bearer <EAM签发的JWT>
```

历史只返回当前 JWT 用户自己的会话和消息。即使 Gateway 已做上下文压缩，历史仍保留用户原始提问与助手回答全文。

## 13. Citation 引用

### 13.1 Citation 详情

```http
GET {BASE_URL}/enterprise/api/v2/citations/{citationId}
Authorization: Bearer <EAM签发的JWT>
```

返回的重点字段：

|字段|含义|
|---|---|
|`citationId`|引用编号|
|`title`|文件名或标题|
|`externalDocumentId`|EAM 文档编号|
|`sourceVersionId`|文档版本|
|`pageNo`|PDF 页码，从 1 开始|
|`bbox`|页面位置，可能为 `null`|
|`assetId`|设备或资产标识|
|`excerpt`|引用原文摘要|

### 13.2 Citation 原始 PDF

```http
GET {BASE_URL}/enterprise/api/v2/citations/{citationId}/source
Authorization: Bearer <EAM签发的JWT>
Accept: application/pdf
```

完整下载返回 HTTP `200`。支持单个 HTTP Range 请求并可能返回 HTTP `206`。该接口不会返回真实文件服务器路径。

## 14. 错误和重试

统一错误格式：

```json
{
  "code": "DOCUMENT_SOURCE_NOT_FOUND",
  "message": "Document source is unavailable",
  "requestId": "request-001",
  "retryable": true
}
```

常用错误：

|HTTP|错误码|处理建议|
|---:|---|---|
|401|`AUTH_SIGNATURE_MISSING`|检查 HMAC 三个请求头|
|401|`AUTH_SIGNATURE_INVALID`|检查 secret、raw body、path、query 和 method|
|401|`AUTH_TIMESTAMP_INVALID`|同步 EAM 服务器时间|
|401|`AUTH_REPLAY_DETECTED`|重试时生成新的 timestamp/signature|
|401|`AUTH_TOKEN_INVALID`|检查 JWT 签名、issuer、audience、算法和 claims|
|403|`AUTH_BINDING_DENIED`|检查 tenant/source 是否为交付值|
|403|`AUTH_USER_DISABLED`|用户已被停用；合法新用户会 JIT 自动开通，正常路径不应再出现 `AUTH_USER_MAPPING_MISSING`|
|403|`ACL_DENIED`|当前用户无设备或文档权限|
|404|`DOCUMENT_NOT_FOUND`|文档任务不存在|
|404|`CONVERSATION_NOT_FOUND`|会话不存在或不属于当前用户|
|409|`EVENT_ID_CONFLICT`|同一 eventId 不得对应不同 payload|
|409|`DOCUMENT_VERSION_CONFLICT`|同一版本不得对应不同 PDF 内容|
|409|`CLIENT_MESSAGE_ID_CONFLICT`|同一消息 ID 不得对应不同问题|
|422|`DOCUMENT_METADATA_INVALID`|检查 metadata 必填字段和身份一致性|
|422|`DOCUMENT_SOURCE_NOT_FOUND`|检查文件共享相对路径和权限|
|503|`RAGFLOW_UNAVAILABLE`|联系知识库系统检查解析、索引或问答服务|

重试规则：

- `retryable=true` 时使用退避重试；
- HMAC 请求每次重试必须重新签名；
- 业务幂等重试保持 `eventId` 或 `clientMessageId` 不变；
- 同一幂等 ID 换了业务内容不能重试，必须使用新 ID；
- 不要对所有 4xx/5xx 无条件重试。

## 15. 双方交付清单

### 15.1 知识库系统提供给 EAM

- [ ] 测试或正式 Gateway 地址；
- [ ] `tenantId`；
- [ ] `sourceSystem=EAM`；
- [ ] `storageRootId`；
- [ ] HMAC `keyId`；
- [ ] HMAC secret，使用独立安全渠道；
- [ ] EAM 侧 `document_type` 名称及变更规则；
- [ ] 30 服务器可访问的文件共享挂载方式、目录映射和权限要求；
- [ ] 联调时的 `requestId` 错误反馈渠道。

第二阶段问答 JWT 参数（issuer / audience / JWKS / kid / `sub` 规则）已确认，见 `eam-inquiry-handoff.md` §3；**无需**再交开通名单。

### 15.2 EAM 提供给知识库系统（第一阶段）

- [ ] 文件服务器访问方式和目录映射；
- [ ] EAM 文档类型名称直接传入 `document_type` 的规则；
- [ ] 设备 ID、固定资产编号和文档版本的 EAM 业务规则；
- [ ] 31 到 30 的文件共享网络连通性、写入完成和原子改名规则；
- [ ] 第一阶段不要求新增 Asset Registry HTTP API。

### 15.3 EAM 提供给知识库系统（第二阶段，可选）

- [x] EAM 的 JWT issuer（`http://192.168.30.31:5105`）；
- [x] JWT audience（`tyrag-gateway`）；
- [x] JWKS 地址（主/备已确认）；
- [x] JWT 签名算法和当前 `kid`（`RS256` / `eam-rs256-1`）；
- [x] `sub` 的稳定生成规则（EAM 用户 id，不变）；
- [ ] 部门、用户组和安全等级编码与投喂 metadata 对齐（方案 1）；
- [ ] ~~联调用户开通名单~~（已取消；JIT 自动开通）。

## 16. 联调验收清单

### 16.1 第一阶段文件入库

- [ ] EAM 能将测试 PDF 写入约定的相对路径；
- [ ] EAM 能计算实际 PDF SHA-256；
- [ ] HMAC 正常登记返回 HTTP `202` 瘦身受理回执（无 `statusUrl`）；
- [ ] EAM 回调 URL 已配置，并能接收终态通知；
- [ ] 收到 `status=retrievable`（或诊断确认 `retrievable=true`）；
- [ ] 文档最终达到可检索；
- [ ] 错误 secret 返回 `AUTH_SIGNATURE_INVALID`；
- [ ] 重放相同签名返回 `AUTH_REPLAY_DETECTED`；
- [ ] 错误 tenant/source 返回 `AUTH_BINDING_DENIED`；
- [ ] 重复 `eventId` 和相同 payload 能幂等；
- [ ] FILE_SHARE v3 登记不依赖 EAM 资产解析 GET；
- [ ] Gateway 能从 30 挂载的 31 文件共享读取 PDF；
- [ ] PDF 解析、索引和质量检查完成后达到 `qualityStatus=passed`；

### 16.2 第二阶段用户问答（可选）

- [ ] EAM 已签发 RS256 JWT，并提供 JWKS / iss / aud；Gateway 仅配置公钥侧；新 `sub` JIT 自动开通；
- [ ] EAM JWT 能创建设备会话，且 create 响应含匹配上下文的 `suggestions`；
- [ ] JWT ACL claims 与投喂 metadata 对齐后可检索到对应设备文档；
- [ ] 点选 chip（`suggestionId` + `contextVersion`）与自由 `question` 均可问询；
- [ ] 同 `conversationId` 续问与历史恢复正常；
- [ ] 正常问题返回 `status=completed`；
- [ ] 回答包含正确的 `externalDocumentId`、`sourceVersionId` citation；
- [ ] 无权限用户不能访问其他设备文档；
- [ ] 用户 A 不能读写用户 B 的会话；
- [ ] EAM 不直接访问 RAGFlow、数据库、Redis、对象存储管理端口或内部 ticket 接口。

完整契约文件：

- `contracts/file-share-v3.yaml`
- `contracts/integration-openapi-v2.yaml`
