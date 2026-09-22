# F11：可靠回放、附件隔离和受控偏好记忆

状态：本地实现，真实 PostgreSQL / RAGFlow / Memory API 集成未验收；未部署。

## 行为

回放和历史使用持久化消息/引用快照，重新校验当前权限和文档版本、生成新链接。不复用旧 `_public_citations` 或 `_streamDeltas`；找不到规范快照则隐藏引用。撤权仅隐藏引用，正文、业务状态和稀疏编号保留。投影失败隐藏引用，不返回内部原始快照，也不把已提交 run 改判失败。新 run 结果保存规范引用；首次成功响应在终态提交后生成链接。

Chat/Workflow 回放均遵守 Accept；失败 SSE 保留安全正文和引用后只发 run.failed，JSON 保留非 2xx。租约恢复路径也走同一投影。

G 与当前合法附件 A 独立，仅 G/A 同时为空时短路。上游 `dialog_service::rag_agent/async_chat` 在 restrict + G=[] + files 时进入已有 solo 文件解析/生成路径；不进入向量、关键词、SQL、图谱和网络工具。Workflow 使用现有附件分支，不修改 New2.json，业务状态仍来自现有终态。此分支是最小上游补丁，不能在升级时丢失空 G 限制。

## 偏好与持久化

仅支持 language={zh,en}、detail={brief,detailed}、format={paragraphs,list}。完整用户句式“以后/今后（都）（请）+偏好”才生成候选，例如“以后请简短回答”；引号、否定、歧义、临时要求和技术事实不生成候选。不调用新模型，也不从回答或文档提取偏好。

候选与非失败 run 的终态同事务提交，run/key 去重，7 天有效。用户确认有 revision CAS，重复确认幂等，旧 revision 冲突。删除立即使本地注入失效。前端在终态后读取候选，确认/忽略/删除独立于回答正文；修改可重新表达偏好并确认。

企业 schema 10→11 新增 `ext_user_preference`、`ext_preference_candidate`、`ext_preference_outbox`，不改官方模型/迁移或依赖锁。本地有限值是唯一注入真相源，当前问题要求优先；旧 Memory 问答不注入、不导入、不物理删除。旧 fire-and-forget 入口成为不写入的兼容入口。

确认/删除与 outbox 同事务。worker 用数据库时钟、SKIP LOCKED、不可重用 claim_id、60 秒租约；网络操作不持行锁；单次投递 30 秒、至多 8 次，指数退避上限 3600 秒。过期租约可恢复，旧 revision 不再投递。按用户/偏好键的修订顺序交付；不同键互不影响。旧池也安排清理。

官方 Memory API 没有接收幂等键，故仅承诺有界至少一次尝试，不承诺 exactly-once。独立 agent 命名空间隔离用户/偏好键，稳定 event_id 标记投递，重试先清理该命名空间旧副本。镜像从不参与回答注入；延迟写入/重复镜像不能让未确认或已删除偏好重新生效。超过重试预算保留 dead 记录，管理员排查后可定点恢复 pending，禁止批量删除。

开关关闭时停止候选、注入和远程投递，保留记录及可删除能力。启用但远程池缺失时，既有本地确认偏好仍可使用，新确认返回 MEMORY_NOT_CONFIGURED；远程恢复后继续投递。

## 契约与验收

新增受 Token 身份限定接口见 [契约](../docs/contracts/f09-f11-preferences.md)。问答 JSON/SSE 字段及业务状态枚举不变，生产 Workflow 不变。

测试：无服务 F11 控制测试；`enterprise/tests/test_preference_pg.py`（真实连接并发确认、事务回滚、隔离、去重和修订冲突）；更新 `test_user_memory.py`，前端 PreferencePanel/HarnessChat，F01–F07/F12 回归。

待验收：Python3.13 固定依赖、两 worker 与租约恢复、官方 Memory 异步落地/重复/删除契约、真实附件上传解析和权限负向、Chat/Workflow JSON/SSE/历史端到端。合成调用链验证不能替代这些项目。

上线：先排空旧 worker，迁移企业 schema，再配套更新 Gateway、上游最小补丁和企业前端。回滚应继续禁止旧 QA 注入与旧临时链接回放；不可启动仍自动导出完整问答的旧 worker。未执行生产部署/历史清理。
