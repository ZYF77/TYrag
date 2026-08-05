# 04 身份、SSO、RBAC 与 ACL

## 1. 身份方案

### 客户业务入口

用户在客户系统登录，业务前端调用 Enterprise Gateway，并携带客户签发的 Token。Gateway：

1. 验证签名、issuer、audience、过期时间和租户；
2. 解析 `user_id`、部门、角色和其他稳定声明；
3. 按需查询业务权限服务；
4. 映射至 RAGFlow 资源身份；
5. 不把客户 Token 直接暴露给浏览器之外的第三方服务。

### RAGFlow 管理后台

管理员可使用 RAGFlow 原生 OAuth2/OIDC 配置登录。生产上线前必须在固定版本上验证：首次用户创建、禁用用户、邮箱变化、退出、Token 过期和回调 URL。

## 2. 用户映射表

建议企业数据库表：

```text
ext_user_map
- tenant_id
- business_user_id
- ragflow_user_id
- business_subject
- status
- created_at
- updated_at
```

不得保存客户密码。RAGFlow 用户映射失败时必须拒绝访问，不能回退为共享管理员账号。

## 3. 角色

最少角色：

- `end_user`：问答、本人会话、授权引用；
- `knowledge_maintainer`：同步状态、metadata、复核、重试；
- `system_admin`：模型、知识库、权限、部署和升级；
- `auditor`：审计元数据，正文仍需文档 ACL。

## 4. ACL 权威来源

业务 PG/权限服务负责：

- 租户；
- 部门和角色；
- 可访问设备/项目；
- 文档密级；
- allow/deny 组；
- 文档有效状态。

RAGFlow metadata 和 dataset/team 权限是执行载体，不是全部权威。

## 5. 三道防线

### 防线 A：入口权限

校验用户是否允许使用知识问答、指定设备和指定知识库。

### 防线 B：召回前过滤

将权限编译成：

- allowed dataset IDs；
- document IDs（仅数量可控时）；
- metadata filters：tenant、department、equipment、security level、status；
- 禁止先全库召回再后删。

### 防线 C：返回前复核

引用、PDF、页图、图片、表格和历史消息返回前，再用业务 PG 当前权限复核。权限变化不应等待重新 Embedding 才生效。

## 6. deny 优先规则

P0 冻结的 ACL 决策规则见 [`contracts/acl-design-freeze.md`](../contracts/acl-design-freeze.md)，
空部门/密级/allow 组等未决输入一律按 `UNRESOLVED` 拒绝，不构成公开访问。

默认：

1. 租户不匹配直接拒绝；
2. 文档 disabled/superseded/review_required 不可检索；
3. security_level 超出用户等级拒绝；
4. `deny_group_ids` 命中优先于 allow；
5. 有 allow 规则时至少命中一项；
6. 无规则文档不得默认全员可见，除非知识库明确 public。

## 7. 必测越权路径

- 检索 API；
- Chat API；
- 文档详情；
- 原文件下载；
- Range 请求；
- 页图、缩略图和资产；
- 会话读取和删除；
- documentId、sessionId、assetId 枚举；
- 管理 API；
- 缓存命中后的权限变化。
