# 本地设备联调：Postman 最小 P0

本包只覆盖本地设备模拟所需的最短链路：

`FILE_SHARE v3 登记 → 读取响应 statusUrl 轮询 → /enterprise/api/v2 创建会话 → 两轮消息 → 历史 → citation/source`

Collection：`enterprise/postman/tyrag-device-integration.postman_collection.json`
Environment template：`enterprise/postman/tyrag-local.postman_environment.template.json`

## 1. 生成 Newman/CLI 本地环境

不要把 secret 写进仓库、Collection 或导出的 Environment。当前本地 Gateway overlay 默认地址是 `http://127.0.0.1:5188`；如需其他地址，用 `--base-url` 覆盖。由主线程按真实部署配置提供 `TYRAG_JWT_SHARED_SECRET` 和 `TYRAG_HMAC_SECRET`，然后运行：

```powershell
$env:TYRAG_JWT_SHARED_SECRET = '<process-local-value>'
$env:TYRAG_HMAC_SECRET = '<process-local-value>'
python enterprise/scripts/generate_postman_local_environment.py `
  --file <FILE_SHARE-root>\manual.pdf `
  --output enterprise/postman/tyrag-device.local.postman_environment.json
```

输出文件名必须以 `.local.postman_environment.json` 结尾，已被 `enterprise/postman/.gitignore` 忽略；脚本不会打印 JWT 或 HMAC secret。脚本会计算 PDF SHA-256 和大小，并生成 Gateway 现有 HS256 测试 claims。生成环境默认包含 `pollAttempt=0`、`maxPollAttempts=10`。

```powershell
npx newman run enterprise/postman/tyrag-device-integration.postman_collection.json `
  --environment enterprise/postman/tyrag-device.local.postman_environment.json `
  --delay-request 2000
```

手工 Postman 优先在 Vault 中保存 `hmacSecret`、`userJwt`。Collection 的 HMAC pre-request script 先读 `pm.vault`，只有 CLI/Newman 或未配置 Vault 时才回退读取环境变量。模板中的两个 secret 值始终为空。

## 2. 身份与绑定

- FILE_SHARE v3 是 HMAC-only：`X-TY-Timestamp`、`X-TY-Key-Id`、`X-TY-Signature: v1=<lowercase hex>`；不发送 Bearer。签名 canonical input 是 `v1`、十位 epoch、uppercase method、规范化 path+排序 RFC3986 query、精确 raw body 的 SHA-256，各占一行。
- HMAC credential 必须在 Gateway 配置中绑定同一 `(tenantId, sourceSystem)`；请求 body 的 tenant/source 也必须匹配，不要把 secret 放在请求中。
- v2 正式问询只使用 `Authorization: Bearer {{userJwt}}`。JWT 使用 Gateway 已有测试 HS256 机制：`sub`、`tenant`、`name`、`department`、`roles`、`groups`、`security_level`、`iat`、`exp`、`iss`、`aud`。本地 Gateway 需启用 HS256 测试配置，并存在 active user mapping；生产客户 JWT 不应复制测试 secret。
- 用户 role `end_user` 提供会话、问询、历史和 citation 能力；tenant、部门、group、security level 和设备 ACL 仍由 Gateway 决定，不能由请求 body 越权覆盖。

## 3. 运行顺序与当前边界

按 Collection 顺序执行。登记测试从响应 JSON 读取相对 `statusUrl` 并原样写入变量；轮询请求使用 `{{baseUrl}}{{statusUrl}}`，只补本地服务 base URL，不改写服务端返回的 path/query。如果 `retrievable` 不是 `true`，Runner/Newman 会把当前 poll 请求重新执行，直到 `maxPollAttempts=120`；请求间应保持约 2000ms delay，超限失败，只有 `retrievable === true` 才继续到 v2 问询。Newman 使用上面的 `--delay-request 2000`；Postman Collection Runner 也应设置约 2000ms request delay。`pm.execution.setNextRequest` 只在 Collection Runner/Newman 中驱动下一请求，普通 Send 不会自动循环。如果服务当前响应缺少 `statusUrl` 或 `retrievable`，测试会明确失败，这是接口缺口，不应手工猜 URL。

FILE_SHARE 源文件不存在可能在异步 worker 阶段才出现 `DOCUMENT_SOURCE_NOT_FOUND`；Collection 另带一个确定性的未登记文档 `DOCUMENT_NOT_FOUND` 示例。本文不宣称 live Postman/Newman 已通过；实际响应、worker 状态和真实 E2E 由主线程在合并后记录。

## 4. 离线校验

```powershell
python enterprise/scripts/validate_postman_artifacts.py `
  enterprise/postman/tyrag-device-integration.postman_collection.json `
  enterprise/postman/tyrag-local.postman_environment.template.json
python -m pytest enterprise/tests/test_postman_artifacts.py -q
```

校验不发网络请求，检查 Postman v2.1 结构、每个请求的 method/URL/header/auth/body/tests、变量引用、空 secret、固定 v3/v2 路由和 HMAC query 编码固定向量。`git diff --check` 仍是提交前必做检查。
