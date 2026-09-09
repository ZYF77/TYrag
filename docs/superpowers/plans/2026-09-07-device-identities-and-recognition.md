# Gateway 设备标识查询与识别规则配置实施方案

> 日期：2026-09-07  
> 范围：Gateway、Enterprise Web、契约与测试；不含 TPM / RAGFlow 上游 / 服务器部署。  
> 来源：Codex 计划三稿合并落地。

## 1. 目标

1. Web Console 展示 EAM 当前有效的三种设备标识，并可跳转文件元数据（按稳定 `equipmentId`）。
2. 对话按「候选编号提取 → 当前标识映射 → equipmentId → ACL/可检索性」识别设备。
3. 开放受限编号提取正则配置；映射 / ACL / 型号排除 / 多设备比较保持代码控制。
4. EAM 更新标识后，Gateway 异步投影到关联 RAGFlow 文档 `meta_fields`（仅 PATCH 元数据，不上传/不解析/不重切）。

## 2. 数据与外部契约

### 当前映射

- 表：`ext_asset_registry`，主键 `(tenant_id, equipment_id)`。
- 可变字段：`fixed_asset_no`、`asset_id`（可空；空串规范化为 NULL；不得补造）。
- `identity_version`：EAM 按设备递增的正整数，独立于文档 `sourceVersionId`。

### EAM 接口

- `PUT /enterprise/api/v3/equipment-identities/{equipmentId}`  
  HMAC 服务鉴权；完整快照；高版本更新；同版同内容幂等；低版本忽略；同版异内容 / 冲突 → 409。  
  接受后同事务写 outbox，响应 **202**（异步投影，不宣称 RAGFlow 已同步）。
- `GET /enterprise/api/v3/equipment-identities/{equipmentId}?tenantId=&sourceSystem=`  
  当前映射 + sync 状态。

OpenAPI：`contracts/equipment-identity-v1.yaml`。

### RAGFlow 投影字段

对关联文档调用现有 `update_document_metadata`（PATCH），写入：

- `equipment_id`
- `fixed_asset_no`（空则删键/省略）
- `asset_id`（空则删键/省略）
- `enterprise_identity_version`

禁止触发 upload / parse / rechunk。旧任务不得用旧快照覆盖新版本。

## 3. 管理接口（admin）

前缀：`/enterprise/api/v1/admin/system`

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/equipment-identities` | 分页精确查询 |
| GET | `/equipment-identities/{id}/sync` | 同步状态 |
| POST | `/equipment-identities/{id}/sync` | 失败/完成重试（复用 outbox，幂等） |
| GET/PUT | `/equipment-recognition` | 识别配置 |
| POST | `/equipment-recognition/preview` | 未保存规则预览 |

识别默认 **开启**（无 DB 行时 `enabled: true`；主开关 + 一条默认正则）。已有 `enabled=0` 租户行保持关闭。

## 4. 正则执行策略（RE2）

计划原拟使用 `google-re2`。Windows 环境引入成本高，当前实现保留 **stdlib `re`**，并：

- 拒绝 `(?` 预查 / 反向引用类构造；
- 最长 512 字符；禁止匹配空串；
- 候选长度 1–128，每次最多 64 个。

若后续 Linux 镜像统一引入 google-re2，可替换执行引擎且保持同一校验边界。

## 5. Web

Console「设备标识」页：设备列表 + 识别规则。同步失败显示「重试同步」。文件跳转仅带 `equipmentId`。

## 6. 验收要点

- 三种标识查询/提问定位同一设备；更新后旧可变标识不再作别名。
- RAGFlow meta 与 Gateway 最终一致；清空字段无残留键；不触发 parse。
- 乱序/幂等/冲突/并发符合契约；admin 重试幂等。
- 识别默认开启；关闭后仅新运行生效；预览与对话共用提取逻辑。已有 `enabled=0` 行保持关闭。
- 跨租户、来源越权、非管理员拒绝。

EAM 对接短文：`docs/integrations/eam-equipment-identity.md`。
