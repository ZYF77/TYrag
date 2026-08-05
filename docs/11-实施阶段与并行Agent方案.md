# 11 实施阶段与并行 Agent 方案

## 1. 阶段 0：事实验证与基线冻结

Lead 单独完成，其他 Agent 不得并行修改共享文件。

交付：

- 固定 tag、commit、image digest；
- 本地启动和迁移成功；
- 真实 PDF 解析 POC；
- 确认文档引擎；
- RAGFlow API 能力矩阵；
- 企业目录脚手架；
- OpenAPI、metadata、错误码和状态机 v1；
- 测试环境和无敏感夹具。

退出条件：WP-00 全部通过。

## 2. 阶段 1：P0 并行实现

在 Lead 冻结契约后并行：

| 工作包 | Agent | 主要范围 |
|---|---|---|
| WP-01 | Identity/ACL | SSO、用户映射、RBAC、ACL |
| WP-02 | File Sync | 事件、幂等、版本、停用、状态回写 |
| WP-03 | Parsing | parser profile、真实样本、质量门禁 |
| WP-04 | Retrieval | Chat/Session、业务 PG、综合回答、SSE |
| WP-05 | Frontend | 普通用户、引用抽屉、维护页面 |
| WP-06 | Platform | Compose overlay、安全、审计、备份基础 |

Lead 负责跨模块模型、迁移、配置和 RAGFlow 核心补丁。

## 3. 阶段 2：集成与 Beta

- 设备档案包；
- 批量历史导入；
- 人工复核；
- 删除和版本一致性；
- 评测 UI；
- 监控、备份和大文件测试；
- 真实模型限流和降级。

## 4. 阶段 3：Production

- 高可用；
- 安全和许可证门禁；
- 灾备演练；
- 模型/索引升级；
- 性能 SLO；
- 生产运维手册。

## 5. 并行规则

- 同一共享契约只能由 Lead 修改；
- 每个 Agent 有独占目录；
- 前端基于冻结 OpenAPI 和 Mock 开发；
- 上游核心修改必须串行合并；
- QA 不接受“实现已完成”声明，必须以自动化证据判断；
- 每波结束运行全量契约和 E2E，再进入下一波。

## 6. 里程碑验收

### M0 基线
- 一条命令启动固定版本；
- 官方 UI 和 API 可用；
- 三类 PDF POC；
- 版本清单完整。

### M1 文件与权限
- 文件事件到 ready；
- 幂等和停用；
- 无权限召回和下载为 0。

### M2 问答闭环
- 文档 + 业务记录综合回答；
- Session、设备上下文、SSE 和引用；
- 真实评测集达标。

### M3 Beta
- 复核、批量、审计、备份、监控和大文件。

### M4 Production
- HA、灾备、安全、升级和 SLO。
