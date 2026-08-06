# WP-01 身份、用户映射与 ACL

## Owner
Identity/ACL Agent

## 目标
实现客户 Token 验证、业务用户到 RAGFlow 映射、角色能力、ACL 编译、返回前复核和负向安全测试。

## 依赖
依赖 WP-00 OpenAPI、metadata 和错误码；需要客户 Token 样例及业务权限表说明。

## 交付物
- `enterprise/gateway/auth`；
- `enterprise/gateway/acl`；
- 用户映射表迁移请求；
- ACL policy 单元测试；
- IDOR/越权集成测试。

## 验收
- 无权限检索、Chat、PDF、图片、会话均为 0 越权；
- deny 优先、密级和租户隔离正确；
- Token 过期/issuer/audience 错误拒绝；
- 不保存密码。

## 禁止修改/禁止范围
不得修改 RAGFlow 认证核心、数据库模型和公共 OpenAPI；不得使用共享管理员账号代理所有用户。

## 完成报告
列出修改文件、测试命令、结果、契约变化、上游补丁、风险和后续集成注意事项。
