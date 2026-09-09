# EAM 设备标识对接说明（Gateway）

## 鉴权

复用现有 **服务 HMAC / Bearer** 协议。每次 HTTP 请求按现网规则重新签名；业务重试保持相同 `identityVersion` 与字段内容，但必须重新签名。

绑定校验：`tenantId` + `sourceSystem` 必须落在服务主体允许范围内。

## 版本语义

- `equipmentId`：稳定主键，普通更新接口不可改键。
- `identityVersion`：EAM 为每台设备维护的递增正整数；**独立于**文档 `sourceVersionId`。
- 高版本覆盖；同版同内容 → 200 幂等；低版本 → 200 + `outcome=stale`；同版异内容 → 409。

## 写入与异步投影

`PUT /enterprise/api/v3/equipment-identities/{equipmentId}`

请求（完整快照）：

```json
{
  "tenantId": "...",
  "sourceSystem": "EAM",
  "identityVersion": 12,
  "fixedAssetNo": "FA-...",
  "assetId": "ASSET-..."
}
```

`fixedAssetNo` / `assetId` 可显式置 `null` 清空。

成功接收新版本返回 **202**：Gateway 映射已更新，RAGFlow `meta_fields` 由 outbox 异步 PATCH。响应中的 `syncStatus=pending` 不代表 RAGFlow 已收敛。

查询进度：

`GET /enterprise/api/v3/equipment-identities/{equipmentId}?tenantId=...&sourceSystem=...`

## RAGFlow 元数据（只读了解）

投影字段：`equipment_id`、`fixed_asset_no`、`asset_id`、`enterprise_identity_version`。  
空可变字段删除对应键。仅文档级 meta，不重新上传/解析/向量化。

管理员可在 Console 对失败项 `POST .../admin/system/equipment-identities/{id}/sync` 重试（幂等入队）。

## 建议联调顺序

1. 全量同步当前设备映射；
2. Console 查询与文件跳转；
3. 变更可变标识后确认 Gateway + RAGFlow meta；
4. 按租户启用识别规则后再做对话检索验收。
