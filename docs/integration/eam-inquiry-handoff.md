# EAM 问询对接说明（Gateway Inquiry v2.3）

面向：EAM 开发 / 测试 / 运维  
契约版本：`integration-openapi-v2` **v2.3.0**  
正式契约文件：

- `contracts/integration-openapi-v2.yaml`

EAM **只**对接 Enterprise Gateway 的 `/enterprise/api/v2` 问询接口，不要直接访问 RAGFlow、数据库、Redis、对象存储管理端口或 Gateway 内部接口。

与投喂交接的关系：

| 能力 | 交接文档 | 鉴权 | 路径前缀 |
|---|---|---|---|
| 文件投喂 FILE_SHARE | `docs/integration/eam-file-feed-handoff-3.1.md` | HMAC（`X-TY-*`） | `/enterprise/api/v3/documents` |
| **用户问询（本文）** | 本文 | **User JWT（Bearer）** | `/enterprise/api/v2/conversations` 等 |

HMAC secret、JWT 私钥、RAGFlow API key **必须互相独立**，不可混用。

---

## 1. 本次变更范围（相对早期问询描述）

| 项 | 变更前（旧口径/旧实现） | 变更后（当前交付） |
|---|---|---|
| 设备身份校验 | 创建/提问前可能调用 Asset Registry / EAM GET | **不调用**；直接信任 EAM 提交的 `equipmentId` / `fixedAssetNo` |
| `ASSET_REGISTRY_UNAVAILABLE` | 可能阻断创建会话 | **问询主路径不应再出现** |
| 开场引导 | 未作为正式对外字段 | 创建/详情返回服务端 `suggestions`（chips） |
| 长对话上下文 | 未明确压缩策略 | Gateway **透明**滚动摘要；EAM 仍用原 `conversationId` |
| `assetId` / `registryVersion` | 可能依赖注册表回填 | 问询路径通常为 `null`；勿依赖 |
| SSE | 有契约描述 | Gateway **已实现**；EAM **首期可不接**（见 §7） |
| 消息附件 | 无 | 同一 `POST .../messages`；无附件继续 JSON，有附件改 multipart。**不走 HMAC** |

**不变：**

- 会话/历史/提问仍走 Gateway v2，不直连 RAGFlow
- 正式环境 JWT 由 **EAM 签发**，Gateway 只验签（JWKS）
- 消息业务状态与 `citations` **相互独立**，不得互相推导
- 用户只能访问自己 JWT 下的会话（多用户隔离）

综合总册仍见：`docs/integration/eam-device-integration-guide.md`（§9–13）。协议底稿见：`docs/设备管理系统—企业知识库对接协议.md`。

---

## 2. 总体流程

```text
（前置）文档投喂终态为 retrievable
  → EAM 为当前用户签发 JWT
  → POST /enterprise/api/v2/conversations（可带 equipmentId，也可先建草稿）
  → 渲染响应 suggestions（开场 chips）
  → 用户点选 chip 或自由输入
  → POST .../messages（suggestionId 或 question）
  → 展示 answer / status / citations
  → 同一 conversationId 续问；需要时 GET 历史 / citation
```

说明：

- UI 在 **EAM 侧**；Gateway 只提供 API 与 chips 定义。
- EAM **不**传 RAGFlow `session_id` / `chat_id`；只使用 Gateway 的 `conversationId`。
- 未带 `equipmentId` 的草稿会话可以提问：句中唯一命中已投喂设备号则按该设备检索，否则在当前用户 ACL 可见文档内全局检索，并在回答末尾建议补充设备号。
- 建议仅在对应设备文档已 `retrievable` 后开放问询入口（以投喂回调为准，见投喂 handoff）。

---

## 3. 鉴权（User JWT）

### 3.1 请求头

```http
Authorization: Bearer <EAM签发的JWT>
Accept: application/json
Content-Type: application/json
```

不要用 HMAC 三头，不要用 RAGFlow API key。

### 3.2 正式环境职责（已确认）

| 项 | 责任方 |
|---|---|
| 签发 RS256 JWT、**保管私钥（勿交付知识库）** | **EAM** |
| 发布公钥：提供 **JWKS URL**（及 `iss` / `aud` / `kid`） | **EAM** |
| 拉取 JWKS、本地验签（签名 / iss / aud / exp） | **Gateway** |
| 用户映射 `ext_user_map` | **Gateway JIT**：合法 JWT 首次访问自动开通为 `active`；`disabled` 仍拒绝 |

EAM 已确认并写入 Gateway 的参数：

| 配置 | 已确认值 |
|---|---|
| JWKS（主） | `http://192.168.30.31:5105/.well-known/jwks.json` |
| JWKS（备） | `http://192.168.30.31:5105/api/v1/ai/jwks.json` |
| 算法 / `kid` | `RS256` / `eam-rs256-1` |
| `issuer` (`iss`) | `http://192.168.30.31:5105` |
| `audience` (`aud`) | `tyrag-gateway` |
| 有效期 | 15 分钟（短时签发；浏览器不持私钥） |
| `sub` 规则 | EAM **用户 id**，长期不变（白话见 [`eam-inquiry-sub-notice.md`](./eam-inquiry-sub-notice.md)） |
| claim 字段名 | Gateway **默认**（见 §3.4）；无需 `JWT_CLAIM_MAP` |
| 开通名单 | **不需要**。合法 JWT 首次问询自动 JIT 落库 |

### 3.3 公钥、验签方式（重要）

```text
用户在 EAM 登录
  → EAM 用私钥签发 JWT
  → EAM 调用 Gateway 时带 Authorization: Bearer <JWT>
  → Gateway 按 JWT_JWKS_URL 拉取/缓存公钥，在本地验签
  → 通过后 JIT/读取 ext_user_map，再处理问询
```

要点：

- **要公钥，不要私钥。** 正式环境通过 JWKS 发布公钥即可；私钥只留在 EAM。
- **验签在 Gateway 本地完成。** 每个问询请求**不会**再回调 EAM 做 `validate-token`。
- EAM **不需要**新增“帮知识库验 JWT”的业务接口；需要的是：
  1. 按约定 **签发 JWT**；
  2. 暴露 **JWKS（只读公钥端点）**，且 Gateway（30）网络可达 `.31:5105`。
- 密钥轮换：EAM 更新 JWKS；Gateway 在缓存 TTL 后自动拿到新 `kid` 对应公钥。
- JWKS 与投喂 HMAC secret、RAGFlow API key **互相独立**。

### 3.4 Claims 与 ACL（方案 1：租户 / 组 / 密级对齐；部门不硬匹配）

字段名按 Gateway 默认读取（EAM 已确认）：

| 用途 | 默认 JWT 字段 | 说明 |
|---|---|---|
| 用户主体 | `sub` | 必填；EAM 用户 id |
| 租户 | `tenant` | 联调环境固定 `wp04e2e` |
| 角色 | `roles` | 提问至少含 `end_user` |
| 部门 | `department` | 数组；用户所属部门。**不参与硬 ACL**，不必等于投喂 `department_id` |
| 用户组 | `groups` | 数组；须与文档投喂 `allow_group_ids` 对齐 |
| 安全等级 | `security_level` | 整数；须 ≥ 文档投喂 `security_level` |
| 显示名 | `name` | 可选 |
| 业务用户 ID | `business_user_id` | 可选；缺省则用 `sub` |

**ACL：** JWT 的 `tenant` / `groups` / `security_level` 必须与该用户可见文档的投喂 metadata 一致，否则检索会被 deny-first ACL 拒绝（空范围 → `无可靠依据`，或 `ACL_DENIED`）。问询范围另受会话 `equipmentId` 约束。`department` 可缺省、可与文档部门不一致（用户部门 ≠ 设备归属部门是预期情况）。投喂仍写 `department_id`，只是问询不再拿它做硬拒绝。见 `docs/adr/acl-department-not-hard-deny.md`。

EAM 签发最小 payload 约定（联调常用示例）：

```json
{
  "iss": "http://192.168.30.31:5105",
  "aud": "tyrag-gateway",
  "sub": "<EAM用户id>",
  "iat": 0,
  "exp": 0,
  "tenant": "wp04e2e",
  "roles": ["end_user"],
  "department": ["3"],
  "groups": ["maintenance"],
  "security_level": 2
}
```

说明：`groups` / `security_level` 须与可见文档投喂一致（上表为当前联调常用示例）。`department` 填用户真实部门即可，不必改成设备部门号。`iat`/`exp` 由 EAM 按约 15 分钟有效期填写。

**明确不做：**

- 不把 `admin@ragflow.io` 写进正式业务 JWT
- 不让终端用户自行篡改 claims
- 不直接调用 RAGFlow、不传 datasetId、不覆盖 dataset system prompt
- **不要求** EAM 提供“校验 JWT”的回调/RPC；也不接收 EAM 私钥
- **不要求** EAM 提交开通用户 `sub` 名单

### 3.5 联调参数（30 环境）

| 参数 | 值 | 说明 |
|---|---|---|
| Gateway | `http://192.168.30.30:5188` | 根地址 |
| tenant | `wp04e2e` | 与 JWT `tenant` / 文档租户一致 |
| JWT | EAM RS256 + 上表 JWKS | Gateway 配置 `JWT_ALLOWED_ALGS=RS256`；正式路径不依赖 HS256 |

---

## 4. 当前接口规范（EAM 调用我方）

Base path：`{BASE_URL}/enterprise/api/v2`

### 4.1 创建会话

```http
POST {BASE_URL}/enterprise/api/v2/conversations
Authorization: Bearer <JWT>
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

| 字段 | 要求 |
|---|---|
| `equipmentId` | 建议提供；缺省时草稿仍可提问，Gateway 会尝试从首句抽取或走 ACL 全局检索 |
| `fixedAssetNo` | 可选；若提供则原样保存 |
| `faultCode` | 可选；用于 chips/上下文展示 |

Gateway **不做**跨系统设备一致性回查。设备身份由 EAM 保证。

成功 `201`（节选）：

```json
{
  "conversationId": "conversation-001",
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
    }
  ],
  "contextCompacted": false
}
```

要点：

- `suggestions` 由 Gateway **服务端定义**；EAM 只展示 `label`（可用 `displayPrompt` 作预览）
- 发消息时传 `suggestionId` + 匹配的 `contextVersion`；**禁止**客户端自定义 chip prompt
- `contextCompacted` 仅表示是否做过滚动摘要（排障用）；**不**返回摘要正文
- 也可 `GET .../conversations/{id}/suggestions` 单独刷新 chips
- `PATCH .../context` 成功后 `contextVersion` 递增；旧 chip 会 `409 SUGGESTION_STALE`

### 4.2 提问 / 续问（JSON，推荐首期）

```http
POST {BASE_URL}/enterprise/api/v2/conversations/{conversationId}/messages
Authorization: Bearer <JWT>
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

点选开场选项（与 `question` **二选一**）：

```json
{
  "clientMessageId": "device-message-002",
  "suggestionId": "maintenance",
  "contextVersion": 1
}
```

| 字段 | 要求 |
|---|---|
| `clientMessageId` | 必填；同一问题重试保持不变；新问题必须换新 ID |
| `question` | 自由文本分支 |
| `suggestionId` + `contextVersion` | chips 分支；须与当前上下文版本匹配 |

成功 `200`（节选）：

```json
{
  "conversationId": "conversation-001",
  "clientMessageId": "device-message-001",
  "runId": "run-001",
  "messageId": "assistant-message-001",
  "answer": "维护步骤如下……",
  "status": "已完成",
  "citations": [],
  "replayed": false
}
```

若相同 `clientMessageId` 对应的 run 仍在执行，可能返回 `202` + 同一 `runId`（勿再开第二次执行）。

### 4.2.1 带附件提问（multipart，v2.3）

有文件时走**同一 URL**，不要先调 `POST .../attachments`，也不要在 JSON 里塞 `attachments[].content` base64。鉴权仍是问询 JWT，**不要加 HMAC 三头**。

```http
POST {BASE_URL}/enterprise/api/v2/conversations/{conversationId}/messages
Authorization: Bearer <JWT>
Content-Type: multipart/form-data
```

- `metadata`：JSON，`{ "clientMessageId": "...", "question": "..." }`；`question` 可省略（只发文件合法）
- `files`：原始字节；最多 **5** 个，单文件 **10MB**
- 第一波 MIME：`image/jpeg`、`image/png`、`text/plain`、`application/pdf`
- chips（`suggestionId`）**禁止**带文件；请继续用 JSON
- 超限：`413`；类型不支持：`422`

历史 `GET .../messages` 只回附件元数据（`attachmentId` / `fileName` / `mediaType` / `sizeBytes` / `sha256`），**不回文件字节、不嵌下载 URL**。

从图/OCR 抽出的短码只用于检索 enrichment，信任级别是观察（`observed`），不是台账字段。回答应写成「从你上传的图片中识别到疑似故障码 E07」，不要写成「设备当前故障码是 E07」。citations 仍只来自已投喂知识库。

| `status` | 含义 |
|---|---|
| `已完成` | 问答正常完成 |
| `无可靠依据` | 没有足够可靠证据 |
| `失败` | 执行失败 |
| `处理中` | 同一 `clientMessageId` 的 run 仍在执行（HTTP 202） |

**禁止**用 `citations` 是否为空改判 `status`，也禁止用 `status` 推断引用列表。

续问始终使用**同一个** `conversationId`。长对话压缩对 EAM 透明，历史 GET 仍保留用户原文与助手回答全文。

### 4.3 会话列表 / 详情 / 消息历史

```http
GET {BASE_URL}/enterprise/api/v2/conversations?limit=20&cursor=<nextCursor>
GET {BASE_URL}/enterprise/api/v2/conversations/{conversationId}
GET {BASE_URL}/enterprise/api/v2/conversations/{conversationId}/messages?limit=20&cursor=<nextCursor>
Authorization: Bearer <JWT>
```

- 列表项为摘要，**不含** chips
- 详情含当前 `suggestions` 与 `contextCompacted`
- 历史仅当前 JWT 用户自己的会话；归档后可读不可继续写入
- 分页为 opaque cursor：`items` / `nextCursor` / `hasMore`

### 4.4 Citation

```http
GET {BASE_URL}/enterprise/api/v2/citations/{citationId}
Authorization: Bearer <JWT>
```

| 字段 | 含义 |
|---|---|
| `citationId` | 引用编号 |
| `title` | 文件名或标题 |
| `externalDocumentId` | EAM 文档编号 |
| `sourceVersionId` | 文档版本 |
| `pageNo` | PDF 页码（从 1） |
| `bbox` | 页面位置，可能为 `null` |
| `excerpt` | 引用摘要 |

原 PDF：

```http
GET {BASE_URL}/enterprise/api/v2/citations/{citationId}/source
Authorization: Bearer <JWT>
Accept: application/pdf
```

支持单 Range（可能 `206`）。**不**返回真实文件服务器路径。

---

## 5. 错误码与重试（问询相关）

统一错误体：

```json
{
  "code": "AUTH_TOKEN_INVALID",
  "message": "...",
  "requestId": "...",
  "retryable": false
}
```

| HTTP | code | 常见原因 | 建议处理 |
|---:|---|---|---|
| 401 | `AUTH_TOKEN_INVALID` | JWT 签名/iss/aud/算法/`nbf` 未生效（含双方时钟差） | 看 `message`：`Token is not yet valid (nbf)` 时先对时再重签；核对 JWKS |
| 403 | `AUTH_USER_DISABLED` | 用户映射被停用 | 联系知识库侧恢复账号；正常路径不应出现 `AUTH_USER_MAPPING_MISSING`（合法 JWT 会 JIT 开通） |
| 403 | `ACL_DENIED` | 无权访问该设备/文档 | 核对 JWT 租户 / `groups` / `security_level` 是否与投喂一致；部门不匹配不再因此码拒绝 |
| 404 | `CONVERSATION_NOT_FOUND` | 会话不存在或不属于当前用户 | 勿跨用户猜 ID |
| 409 | `CLIENT_MESSAGE_ID_CONFLICT` | 同一 messageId 对应不同问题内容 | 换新 `clientMessageId` |
| 409 | `SUGGESTION_STALE` | chip 与当前 `contextVersion` 不匹配 | 重新 GET suggestions 再点选 |
| 409 | `CONVERSATION_CONTEXT_STALE` | 上下文版本冲突（如并发 PATCH） | 拉详情后按新版本重试 |
| 422 | `CONVERSATION_CONTEXT_REQUIRED` | 仅 v1 无设备提问 | v2 草稿可直接提问；建议补设备号以缩小范围 |
| 422 | `VALIDATION_ERROR` | 请求体非法（如同时传 question 与 suggestion） | 按契约二选一 |
| 503 | `RAGFLOW_UNAVAILABLE` / `RAGFLOW_API_INCOMPATIBLE` | 上游问答暂不可用 | 退避重试；联系知识库侧 |

重试规则：

- `retryable=true` 时退避重试
- 业务幂等重试保持同一 `clientMessageId`
- 同一 ID 换了问题内容 → 必须换新 ID
- 模型问询可能明显长于普通 HTTP；客户端超时由双方部署约定（建议 ≥ 120s）

---

## 6. EAM 改造清单

### 必须做

- [ ] 正式环境签发 RS256 JWT；**私钥自持**；JWKS / iss / aud / kid 已按 §3.2 对接
- [ ] JWT claims 按 §3.4 默认字段名签发；`groups` / `security_level` 与投喂对齐；`department` 填用户真实部门即可
- [ ] 保持 JWKS 公钥端点 Gateway 可达（**无需** validate-token 接口，**无需**开通名单）
- [ ] 请求一律打到 Gateway v2，携带 `Authorization: Bearer`
- [ ] 创建会话建议提交 `equipmentId`（也可先建草稿，在问题里写设备号或走全局检索）
- [ ] 渲染服务端 `suggestions`；点选传 `suggestionId` + `contextVersion`
- [ ] 支持自由 `question` 与同 `conversationId` 续问
- [ ] 用 `clientMessageId` 做提问幂等
- [ ] 按消息 `status` 展示结果，不按 citation 数量改判
- [ ] 会话/历史按当前登录用户隔离展示

### 建议做

- [ ] 仅在投喂回调 `retrievable` 后开放该文档/设备问询
- [ ] 展示 citation 的 `externalDocumentId` / 页码，并支持打开 `/citations/{id}/source`
- [ ] 记录 `requestId` / `conversationId` / `runId` 便于联调排障
- [ ] UI 对 `无可靠依据` 给出明确空证据提示

### 不要做

- [ ] 不要把 JWT **私钥**或 HS 共享 secret（正式环境）交付给知识库或写入前端
- [ ] 不要为 Gateway 验签再单独做“校验 JWT”回调接口（Gateway 本地验签）
- [ ] 不要直连 RAGFlow 或使用运维 admin 账号冒充业务用户
- [ ] 不要传 RAGFlow `session_id`，也不要在压缩后“换会话 ID”
- [ ] 不要自定义 chip 的实际提问文案（只许用服务端 `suggestionId`）
- [ ] 不要在问询请求里使用 HMAC 或混用投喂 secret
- [ ] 不要依赖问询路径的 `assetId` / `registryVersion` 非空
- [ ] 不要因为看不到 RAGFlow 某个手动 Chat 就认为未落库（记录在 Gateway 管理的 `enterprise-formal-{tenant}` 助手下）

---

## 7. 是否要支持 SSE 流式输出？

| 项 | 结论 |
|---|---|
| Gateway 是否已支持 | **是**。同一 `POST .../messages`，`Accept: text/event-stream` |
| EAM 首期是否必须做 | **否**。首期用 JSON（`Accept: application/json`）即可完成联调与上线 |
| 何时再接 SSE | EAM UI 需要打字机逐字展示、且能处理断流/重放时再做 |

SSE 事件顺序（契约）：

```text
run.started
  → 0..n × (answer.delta | citation)
  → answer.completed  或  run.failed
```

注意：

- 没有单独的 `:stream` 第二路由
- run 仍在执行时的重复请求：返回 `202` 状态 JSON，**不会**再开第二条 SSE
- 业务字段（`status` / `citations`）以最终完成事件或后续 GET 历史为准，规则与 JSON 模式相同

**建议给 EAM 的产品决策：** 第一阶段 JSON；第二阶段按体验需要增量接 SSE，不影响会话模型与 `conversationId`。

---

## 8. 最小联调验收

1. 合法 JWT（RS256，JWKS 公钥可验）→ `POST /conversations` 返回 `201`，含 `equipmentId` 与 `suggestions`；新 `sub` 首次访问自动 JIT 开通  
2. 已停用用户（`ext_user_map.status=disabled`）→ `403 AUTH_USER_DISABLED`  
3. 非法/过期 JWT 或 JWKS 不可达导致验签失败 → `401 AUTH_TOKEN_INVALID`  
3b. JWT `groups` / `security_level` / 租户与投喂不对齐 → 问询无可用文档 / `ACL_DENIED`；部门不一致不应单独导致无依据  
4. 点选 chip（`suggestionId` + `contextVersion`）→ `200`，`status` 为 `已完成` / `无可靠依据` / `失败` 之一  
5. 自由中文提问 → `200`，同会话第二次续问成功  
6. `GET .../messages` 能看到用户原文与助手回答  
7. 用户 A 不能读写用户 B 的 `conversationId`  
8. （可选）`Accept: text/event-stream` 能收到 `run.started` 与终态事件  
9. citation 详情/PDF source 在有引用时可用  
10. 问询全程不出现因 Asset Registry 导致的 `503 ASSET_REGISTRY_UNAVAILABLE`

---

## 9. 相关文档

| 文档 | 用途 |
|---|---|
| 本文 | EAM 问询变更与对接总览（与投喂 handoff 成对） |
| `docs/integration/eam-inquiry-sub-notice.md` | **给 EAM 的 `sub` 白话说明**（规则；无需开通名单） |
| `docs/integration/eam-file-feed-handoff-3.1.md` | 文件投喂 + 终态回调 |
| `docs/integration/eam-device-integration-guide.md` | 综合对接总册（含问询细节示例） |
| `docs/设备管理系统—企业知识库对接协议.md` | 协议/验收底稿 |
| `contracts/integration-openapi-v2.yaml` | 问询正式 OpenAPI（v2.3.0） |
