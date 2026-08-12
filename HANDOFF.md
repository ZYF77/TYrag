# TYrag 设备管理系统联调任务交接检查点

> 检查点日期：2026-08-12（Asia/Shanghai）
>
> 本文件用于让新的 Codex Agent 在看不到原对话时恢复当前任务。2026-08-12 已恢复并完成 W1-W4 代码集成；真实外部依赖联调仍未完成，因此不代表客户 Integration Gate 已通过。

## 1. 恢复结论

- 当前主工作树：`C:\CodingProgram\WAES\TYrag`。
- 当前分支：`codex/device-integration-plan`。
- 最新实施提交：`83ed2dc`；若本文件已单独提交，实际 HEAD 会再多一个文档 commit，请以 `git rev-parse HEAD` 为准。
- 在提交本次 HANDOFF 更新前，相对 `origin/codex/device-integration-plan` ahead 10 commits；本文件提交后应为 ahead 11。
- `master` / `origin/master` 仍在 `ae47e86`；不要把当前执行分支误认为 master。
- 主工作树的 tracked 文件没有未提交修改；当前只有未跟踪目录 `output/` 和 `tmp/`。
- `output/`、`tmp/` 是测试/验收运行产物，包含无效或临时内容，当前不得加入提交，也不要批量删除。
- 已创建交接 checkpoint `008ecab chore: checkpoint current implementation before codex handoff`。
- W1、W2、W3、W4 均已由 Luna max 完成、自审、测试并由 Lead 集成；相关 worktree 当前无未提交修改。
- 本轮没有 push、reset、checkout 或丢弃已有修改。

当前最重要的事实：固定 profile、formal 一对一身份、正式 Attachment runtime 和 Console 已进入当前分支并通过离线/Stub/构建 Gate；但 preflight 显示真实 FILE_SHARE、共享数据库、HMAC、RAGFlow、Asset Registry、Gateway、Redis 均未配置，所以不能宣称 E2E-01/E2E-02 或客户正式 Integration Gate 已通过。

## 2. 本轮任务原始目标

依据 `docs/设备管理系统联调优先分阶段实施计划.md`，目标是优先跑通设备管理系统的最短公开联调闭环：

```text
设备管理系统 Backend
  -> Enterprise Gateway
  -> FILE_SHARE v3 文档登记
  -> 服务端固定 parsing profile 下的真实解析、Chunk、Index
  -> 文档状态轮询
  -> formal v2 Conversation / Ask
  -> History / Citation
```

本阶段还包含两个旁路能力，但不能阻塞文件/问询主链：

1. Transient Attachment 按 OpenAPI v2.1.0 提升为正式公开、可运行能力；不再隐藏，不替代 FILE_SHARE 持久文档入口。
2. Enterprise Console 放入本阶段，作为独立联调诊断入口；Console 构建或局部故障不得改变后端主链行为。

执行原则是“先对接跑通，后续再优化”。不在本阶段顺手切换认证、替换基础设施、改上游 RAGFlow、增加正式 migration 或扩展高级产品能力。

## 3. 本阶段验收标准

最终封版至少应满足以下条件；当前 checkpoint 尚未满足全部条件：

- 测试环境可重复启动，preflight 能区分配置缺失和服务故障。
- 设备模拟器只调用公开 Enterprise Gateway，不调用 RAGFlow 内部接口。
- FILE_SHARE v3 登记、稳定 `statusUrl`、轮询、真实解析、Chunk、Index、质量门和真实检索可重复完成。
- `retrievable=true` 只有在真实检索准入条件满足时才成立；citation 能回到外部文档、版本和可解释位置。
- formal v2 能完成创建会话、两轮问询、同会话续问、history 和 citation；Gateway 重启后状态可恢复。
- User B 和其他 tenant 不能读取、追加或通过 citation 访问他人的会话/文档。
- `equipmentId` 与 `fixedAssetNo` 由 Asset Registry 权威解析为同一且唯一的 canonical identity；缺失、冲突、跨 tenant、脏数据和映射漂移必须 fail closed；不得从文档 metadata 创建 alias。
- 文件入库使用服务端固定、可版本化、可读回验证的 parsing profile；客户端不能覆盖 parser/OCR/embedding/profile；实际 parser readback 未验证时不得通过质量门或进入检索。
- Transient Attachment 正式三路由可见并可运行，验证 ownership、tenant 隔离、大小/MIME、TTL、一次性下载票据、清理和 `indexPolicy=never`；Attachment 故障不得影响 FILE_SHARE、conversation、ask、history、citation。
- Console 只调用公开 Gateway 路由，能区分 configured/healthy/unavailable/unauthorized/processing/retrievable/failed；不展示 secret、Token、Cookie、完整 Prompt、原始模型响应或内部 RAGFlow 标识；局部失败不让整页崩溃。
- Contract、P0、相关 Enterprise 测试、Console TypeScript/Vitest/build、必要集成/E2E 均有可解释结果；不得用 `skip`、`xfail`、删除断言或伪造报告绕过失败。
- Postman、Environment template、联调协议和验收 artifact 不包含 secret、token、Cookie、完整连接串或无效临时测试内容。
- 真实依赖与 Stub 边界必须明确；Stub 只能宣称“本地联调基线通过”，不能宣称客户正式 Integration Gate 通过。

## 4. 已完成内容（已在当前分支提交）

### 4.1 执行计划与范围冻结

提交 `501dde7 docs: expand device integration execution scope`：

- 更新 `docs/设备管理系统联调优先分阶段实施计划.md`。
- 明确 T0-T7 工作包、W1-W5 Lead 拆分、验收 Gate、跳过项和后续项。
- 明确本阶段保留 HMAC + User JWT，不迁移 API Key。
- 明确 equipment/fixed asset 一对一、固定 parsing profile、Transient Attachment 不隐藏、Console 纳入但不阻塞主链。
- 明确旧计划与新计划冲突时，以当前 Git、代码、契约和本次证据为准；旧文档中“transient attachment deferred”不能覆盖当前新计划。

### 4.2 Transient Attachment v2.1.0 契约冻结

提交 `0008ce8 feat: freeze transient attachment v2.1 contract`：

- `contracts/integration-openapi-v2.yaml` 升为 `2.1.0`，保持 `/enterprise/api/v2` 路径，采用 additive 变化。
- 冻结正式接口：
  - `POST /conversations/{conversationId}/attachments`
  - `POST /attachments/{attachmentId}/ticket`
  - `GET /attachments/{attachmentId}/download/{ticket}`
- create/ticket 使用 User JWT；download 使用有界 ticket，可匿名，若附带 JWT 则必须与 owner/tenant 一致。
- 冻结 `TransientAttachment`、创建请求、下载参数和稳定错误 envelope。
- 增加/冻结大小、MIME、扩展名、对象完整性、找不到、无权、过期、下载次数、存储损坏/不可用等错误码。
- 下载响应约定 `no-store` 等缓存边界。
- `contracts/error-codes.yaml`、契约冻结文档、计划文档和静态契约 fixture/test 已同步。
- Contract 静态测试及 v2.1 profile runner 在此前运行中均为 23 passed；该证据只证明契约静态一致，不证明运行时已合并。

### 4.3 Asset Registry Stub 一对一边界

提交 `870a71e fix: enforce tenant-scoped asset registry one-to-one mapping`：

- 更新 `enterprise/scripts/asset_registry_stub.py`。
- 更新 `enterprise/ASSET-REGISTRY-STUB.md` 与 `enterprise/tests/test_asset_registry_stub.py`。
- Stub 对 tenant、equipment、fixed asset 的一对一冲突和跨 tenant 情况做 fail-closed 校验。
- W2B 定向测试 8 passed，且做过 Python 编译检查；该提交只覆盖 Stub/runner 边界，不等同于 Gateway formal 查询链已完成。

### 4.4 WP04 测试 fixture 对齐

提交 `d306e11 test: align wp04 fixtures with one-to-one assets`：

- 更新 `enterprise/scripts/wp04_e2e.py`。
- 更新 `enterprise/scripts/wp04_phase2_e2e.py`。
- 让两个 fixture 使用不同且明确的一对一设备身份，避免违反新冻结关系。

## 5. 已完成并集成的并行工作包

- W2 `8af3cd1 fix: enforce tenant-scoped canonical asset identity`：Asset Registry 一对一 canonical identity、旧会话缺失字段补全、映射漂移/冲突/跨 tenant fail closed；工作包 79 passed。
- W3 `7b0cf45 feat: enable formal transient attachment runtime`：正式三路由默认可见、TTL/票据/ownership/清理/完整性/错误 envelope；工作包 105 passed。
- W1 `3b518ed fix: enforce stable parser profile evidence gate`：首次选定 profile/version 在重处理时保持稳定，客户端 override 无效，FILE_SHARE parser evidence/readback 是硬门，非 FILE_SHARE 保留兼容；工作包 180 passed。
- W4 `368fb7d feat(web): add enterprise gateway console`：新增 `/console`，公开 Gateway 服务、FILE_SHARE、会话/history/citation、Attachment 独立诊断；TypeScript、129 项 Vitest 和 production build 通过。
- W3 follow-up `83ed2dc fix: normalize attachment JWT error envelope`：JWT 401 补齐稳定 `retryable:false`；相关回归 63 passed。
- Lead 已逐项审查并按 W2、W3、W1、W4 顺序集成；`app.py` 的 W1/W3 改动自动合并后已回归。

## 6. 尚未开始或尚未完成的工作

- 真实 Integration preflight 当前 blocked：`fileShare`、`database`、`auth`、`ragflow`、`assetRegistry`、`gateway`、`redis` 均为 `missing`。
- 因上述依赖未配置，E2E-01 FILE_SHARE 真实解析/索引/召回/citation 和 E2E-02 两轮问询/重启恢复/权限隔离尚未执行。
- 真实 S3/MinIO Attachment 联调尚未执行；当前只有内存/Stub/ASGI 测试证据。
- W5 尚需在环境就绪后校对 Postman/Environment、运行真实 E2E，并生成不含 secret 的最终验收 artifact；现有 `tmp/`、`output/` 不可作为最终证据提交。
- 客户正式 Asset Registry 的实际响应字段、可用性和一对一数据质量仍需现场验证。

## 7. 关键架构、接口和数据模型决策

这些决策已经写入计划/契约，恢复 Agent 必须继续遵守：

1. 认证保持 HMAC integration identity + User JWT；本阶段不换 API Key，不把 RAGFlow service account 暴露给设备管理系统。
2. Asset Registry 是 `equipmentId`/`fixedAssetNo` 身份唯一权威。Gateway 不从文档 metadata 猜测、创建 alias 或放宽 ACL；同一 tenant 内关系按一对一处理，异常 fail closed。
3. 固定 parsing profile 由服务端决定并带 profile/version；客户端不能覆盖 parser、OCR、embedding 或 profile；只有 parser 执行 evidence 完整且 readbackMatch=true 才能进入质量通过/可检索状态。
4. Transient Attachment 采用用户已确认的方案 A：正式公开 OpenAPI v2.1.0 三接口；conversation scoped；`indexPolicy=never`；默认 TTL 24 小时；create/ticket JWT；download ticket 可匿名但有界，附带 JWT 时必须 owner/tenant 一致；失败清理和稳定错误 envelope 是服务端责任。
5. Attachment 不是 FILE_SHARE 持久文档入口，也不得调用持久 Document Ingestion Pipeline、创建持久 embedding 或跨会话复用。
6. Console 是独立联调诊断页面，不是正式设备业务前端；只调用公开 Gateway 路由；后端和主链不依赖 Console 构建/运行。
7. 本阶段默认不改 RAGFlow upstream、官方迁移、Enterprise 正式 migration、根依赖锁文件和公共 Compose；如确需修改，先提交 Change Request/ADR 并停止当前联调实现。
8. 不提交 `tmp/`、`output/` 下无效测试、截图、运行日志或含环境信息的临时 artifact。

## 8. 当前已知问题和风险

- 当前代码 Gate 通过不等于真实 Integration Gate；RAGFlow、Redis、Gateway、共享 DB、FILE_SHARE、Asset Registry、HMAC 环境缺失是当前唯一已证实的主阻塞。
- 固定 profile 的真实 parser/chunk/index/readback、真实检索和 citation 尚未在当前 HEAD 上连接 RAGFlow 验证。
- Attachment 尚未连接真实 S3/MinIO 验证对象一致性、清理与票据并发行为。
- Console 在无服务侧 HMAC 时会如实显示 FILE_SHARE unauthorized；这不是 Console 故障，也不得把 HMAC secret 下放浏览器。
- `output/`、`tmp/` 存在不可提交运行产物，并且部分路径权限可能导致递归枚举失败；不要为生成 checkpoint 而删除或移动它们。
- 仓库里还有多个历史 worktree/分支（M1/M3/T3 等），不属于本轮 W1-W4；除非新的任务明确要求，不要清理或改写它们。
- 本文件提交后当前分支应 ahead 11，尚未 push；推送前必须再次核对提交范围和远端认证。

## 9. 测试证据和未执行测试

### 本轮已有证据

- 当前 HEAD 定向 Contract/P0/profile/formal/Attachment 集合：284 passed，唯一失败是缺少 RAGFlow live 环境。
- 当前 HEAD 全量 `enterprise/tests`：590 passed，3 failed；失败仅为两项 RAGFlow Integration 环境缺失和一项 Redis Integration 环境缺失。
- W2+W3 首轮集成回归：102 passed。
- W3 JWT envelope follow-up：63 passed。
- Console：`tsc --noEmit` 通过；Vitest 18 files / 129 tests passed；Vite production build 通过。
- `git diff --check` 通过；相对 `master` 没有修改 `ragflow/**`。
- 变更 secret 扫描命中 5 处，均位于测试文件且含明确 test/fixture 标记；未发现真实凭据、私钥或生产 Token。

### 尚未执行或当前不能采信

- RAGFlow live contract 两项、Redis Integration 一项。
- E2E-01、E2E-02、真实 S3/MinIO Attachment、真实客户 Asset Registry。
- 最终 Postman/Newman smoke 和非敏感验收 artifact。

## 10. 恢复后的推荐执行顺序

1. 读取本文件、`AGENTS.md`、实施计划和 OpenAPI v2.1.0；确认仍在 `codex/device-integration-plan`，不要切到 master。
2. 不再重复 W1-W4 实现；先准备非敏感 Integration 环境，让 preflight 的七个依赖由 `missing` 变为 `healthy` 或可解释的 `unavailable`。
3. 环境就绪后执行 E2E-01，再执行 E2E-02；Attachment 真实 S3/MinIO 可并行，但不得阻塞 FILE_SHARE/formal 主链。
4. 对真实失败只修最短链路；若涉及认证模型、主契约、migration、RAGFlow upstream、根锁文件或 Compose，立即停止并请求用户决策。
5. E2E 通过后再校对 Postman/Environment、设备对接协议和最终验收报告；只保存非敏感、可复现证据。
6. 推送前再次执行 `git status`、`git diff --check`、secret scan 和提交范围审计；不得提交 `tmp/`、`output/`。

## 11. 恢复时的安全边界

- 不读取、复制或写入 `.env`、API Key、Token、密码、Cookie、JWT、HMAC secret 或其他敏感值到本文件、日志、测试 artifact 或 Git。
- 不执行 `git reset --hard`、`git checkout --`、批量删除或递归清理；现有 worktree 和提交历史不得擅自改写或清理。
- 不把 `output/`、`tmp/`、PDF/CSV/SQL/日志等测试产物加入提交；只提交源码、测试、契约和必要文档。
- 任何需要修改主契约、数据库 migration、RAGFlow upstream、根锁文件、Compose 或认证模型的请求，先停止并报告决策点。
