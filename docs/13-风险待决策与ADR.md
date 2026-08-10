# 13 风险、待决策与 ADR

## 1. 最高风险

| 风险 | 影响 | 主要控制 |
|---|---|---|
| ACL 越权 | 严重数据泄漏 | 召回前过滤 + 返回前复核 + 负向测试 |
| 上游文件和索引版本不一致 | 错误回答 | 外部版本映射、旧版保留、当前版本复核 |
| OCR 质量不足 | 错误知识 | 真实样本、质量门禁、人工复核 |
| 深度 Fork | 无法升级 | 企业外围层、补丁登记、ADR |
| 文件双份生命周期失控 | 孤儿/旧数据 | 明确业务原件与知识副本职责 |
| 结构化事实向量化 | 精确结果错误 | 业务 PG Query Adapter |
| API 变动 | 集成中断 | Gateway 防腐层、契约测试、版本冻结 |
| 前端隐藏代替权限 | 可绕过 | 所有资源服务端鉴权 |
| 模型外发敏感数据 | 合规风险 | 最小证据、端点白名单、审计 |
| 默认凭据和管理端口 | 入侵风险 | Secret、内网、TLS、扫描 |

## 2. 开发前必须决策

1. 固定 RAGFlow tag/commit/image digest；
2. Elasticsearch 或 Infinity；
3. 上游文件通过 S3 Connector、REST Connector 还是直接文档 API；
4. 是否允许 RAGFlow 保存原文件知识副本；
5. 客户 Token 格式、issuer、audience；
6. 用户和租户映射规则；
7. ACL 权威表和 deny/allow 规则；
8. 第一批业务 PG 表、字段和查询 Adapter；
9. 会话正文真相源；
10. OCR/VLM、Embedding、Reranker、Answer 模型；
11. 第一批真实脱敏 PDF 和评测问题；
12. 设备管理系统接入 TYrag 的部署地址、SSE 代理和 citation 跳转方式（正式 UI 所有权已冻结给设备管理系统）；
13. 文档删除、保留期和审计要求；
14. 生产部署、备份和数据外发要求。

## 3. ADR 模板

```markdown
# ADR-XXX 标题

- 状态：Proposed / Accepted / Deprecated / Superseded
- 日期：YYYY-MM-DD
- 决策人：

## 背景
## 约束
## 备选方案
## 决策
## 正面影响
## 负面影响和风险
## 验证方式
## 回滚方式
## 对上游升级的影响
```

## 4. 必须创建 ADR 的事项

- 文档引擎选择或切换；
- 修改 RAGFlow 内部数据库；
- 修改文档引擎抽象；
- 引入 Qdrant；
- 变更会话真相源；
- 引入 Text-to-SQL；
- 开放 Agent/MCP/Browser/Code；
- 远程模型处理敏感正文；
- 上游核心补丁；
- 生产高可用架构。
