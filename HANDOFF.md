# TYrag 设备管理系统联调任务交接检查点

> 检查点日期：2026-08-12（Asia/Shanghai）
>
> 本文件用于让新的 Codex Agent 在看不到原对话时恢复当前任务。它记录的是当前仓库、Git 状态和已暂停并行 worktree 的事实，不代表本阶段已经封版验收。

## 1. 恢复结论

- 当前主工作树：`C:\CodingProgram\WAES\TYrag`。
- 当前分支：`codex/device-integration-plan`。
- 当前 HEAD：`d306e11`。
- 相对 `origin/codex/device-integration-plan`：ahead 4 commits。
- `master` / `origin/master` 仍在 `ae47e86`；不要把当前执行分支误认为 master。
- 主工作树的 tracked 文件没有未提交修改；当前只有未跟踪目录 `output/` 和 `tmp/`。
- `output/`、`tmp/` 是测试/验收运行产物，包含无效或临时内容，当前不得加入提交，也不要批量删除。
- 本轮 checkpoint 没有执行 commit、push、reset、checkout 或丢弃修改。
- W1、W2、W3、W4 四个相关子任务已经由用户暂停，状态为 `interrupted/idle`；它们的未提交修改仍保留在各自 worktree，尚未合并到当前主工作树。

当前最重要的事实：主分支已经冻结了 v2.1.0 Attachment 契约和 Asset Registry Stub 的一对一测试边界，但 W1/W2/W3/W4 的运行时代码和 Console 仍未由 Lead 审核、提交、合并。因此，当前不能宣称“固定 profile、正式 Attachment、formal 查询一对一、Console 或完整联调已完成”。

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

## 5. 部分完成内容：已暂停的并行 worktree

以下修改均来自基线 `501dde7`，不是当前主工作树的未提交修改。恢复时必须先逐个审查 diff、运行测试、由 Lead 创建聚焦 commit，再按顺序 cherry-pick/合并；不要直接复制目录，也不要假设它们互相兼容。

### W1：固定 parsing profile

- worktree：`C:\Users\Lemon\.codex\worktrees\369e\TYrag`
- HEAD：`501dde7`，detached HEAD；未提交修改仍在现场。
- 修改范围：
  - `enterprise/gateway/parsing/historical_import.py`
  - `enterprise/gateway/quality/gate.py`
  - `enterprise/gateway/quality/router.py`
  - `enterprise/gateway/quality/routing.py`
  - `enterprise/gateway/quality/worker.py`
  - `enterprise/gateway/sync/readiness.py`
  - `enterprise/gateway/sync/sync_service.py`
  - 4 个同主题测试文件：`test_file_share_v3_status.py`、`test_m3e_historical_import.py`、`test_wp03_parser_application.py`、`test_wp03_phase2.py`
- 已实现方向：服务端选择并持久化 profile/version；客户端 override 不生效；SyncService、quality worker/API、readiness、promotion 使用 parser evidence；RAGFlow terminal 配置 readback mismatch 不通过；warn mode 不能绕过 parser 硬门。
- 现场规模：11 个文件，约 350 行净变化（以当前 worktree status/diff 为准）。
- 已报告测试：定向回归 143 项通过；`compileall` 通过；`git diff --check` 通过。
- 未完成：最终自审和聚焦 commit 未完成；尚未在当前主分支集成后做 Lead 统一回归。
- 审查重点：profile 版本在首次入库/重处理间是否稳定；旧数据 `legacy_unverified` 是否会被错误放行；与 W2/W3 的调用边界是否无冲突；是否有任何不必要的上游/契约影响。

### W2：Gateway equipment/fixed asset 一对一

- worktree：`C:\Users\Lemon\.codex\worktrees\2f5d\TYrag`
- HEAD：`501dde7`，detached HEAD；未提交修改仍在现场。
- 修改范围：
  - `enterprise/gateway/asset_registry.py`
  - `enterprise/gateway/query/formal_router.py`
  - `enterprise/tests/test_formal_query.py`
  - 新增但尚未提交：`enterprise/tests/test_w2_asset_registry.py`
- 已实现方向：统一验证 tenant/identifier；equipment-only、fixed-only 和双标识一致解析；跨 tenant/冲突/歧义/漂移 fail closed；formal conversation 创建时 canonicalize，问询前重解析；检索 scope 只接受 canonical identity 精确匹配。
- 已报告测试：formal v1 定向回归 39 项通过；新增 Registry 边界测试在暂停前正在补充，不能视为已通过。
- 未完成：新增 Registry 测试最终运行、diff 自审、聚焦 commit、与主分支集成均未完成。
- 审查重点：不引入 migration；不改变主契约；确认错误码映射与既有 formal v2/v1 调用方一致；确认 candidate identity 过滤不会误伤合法历史 fixture；确认 Registry unavailable/not found/ambiguous/tenant mismatch 的状态码稳定。

### W3：Transient Attachment 运行时

- worktree：`C:\Users\Lemon\.codex\worktrees\955b\TYrag`
- 分支：`codex/w3-transient-attachment`；HEAD `501dde7`；未提交修改仍在现场。
- 修改范围：
  - `enterprise/gateway/app.py`
  - `enterprise/gateway/config.py`
  - `enterprise/gateway/sync/transient_attachment.py`
  - `enterprise/tests/test_config.py`
  - `enterprise/tests/test_transient_attachment.py`
- 已实现方向：默认启用正式三路由；保留显式运维熔断时的 503 `ATTACHMENT_STORAGE_UNAVAILABLE` 且 `retryable=true`；统一 create 错误 envelope；ticket/匿名 download/owner binding；对象完整性错误；上传/ticket 失败清理 retry；文件名响应头安全处理；后台 cleanup worker 无需依赖默认 flag 才启动。
- 已报告测试：定向 Attachment 链路 32 passed；`git diff --check` 通过。
- 未完成：会话和 FILE_SHARE 回归在暂停时仍未完成；最终 self-review、聚焦 commit、主分支合并均未完成。
- 审查重点：运行时响应字段必须与 `0008ce8` 契约一致；正式接口不能返回旧的 501/`ATTACHMENT_NOT_IMPLEMENTED`；attachment 故障隔离不能影响主链；确认默认启用的配置行为与现有部署环境兼容；确认下载 ticket 的单次/次数/TTL/owner 约束。

### W4：Enterprise Console

- worktree：`C:\Users\Lemon\.codex\worktrees\bf38\TYrag`
- HEAD：`501dde7`，detached HEAD；未提交修改仍在现场。
- 修改范围：
  - 已修改：`enterprise/web/src/App.tsx`、`enterprise/web/src/__tests__/V2Client.test.ts`、`enterprise/web/src/api/mocks/handlers.ts`、`enterprise/web/src/api/v2Client.ts`、`enterprise/web/src/api/v2Types.ts`、`enterprise/web/src/components/demo/DemoSidebar.tsx`、`enterprise/web/src/components/harness/TransientAttachmentPanel.tsx`、`enterprise/web/src/components/layout/Sidebar.tsx`、`enterprise/web/src/test-setup.ts`
  - 新增但尚未提交：`enterprise/web/pnpm-workspace.yaml`、`enterprise/web/src/api/consoleTypes.ts`、`enterprise/web/src/pages/EnterpriseConsolePage.tsx`、`enterprise/web/src/pages/enterprise-console.css`
- 已实现方向：`/console`/`VITE_UI_MODE=console` 入口；公开 health/auth/v3 FILE_SHARE client；conversation/history/citation 与 Attachment 诊断入口；模块局部错误隔离；只展示安全摘要，不展示 ticket、download URL、附件内容、secret 或内部 RAGFlow ID。
- 未完成/受阻：worktree 无项目 `node_modules`；离线安装曾生成临时 `pnpm-lock.yaml`，该文件已由子任务移除；TypeScript、Vitest、build 未形成可采信通过证据；最终 self-review、commit、主分支合并未完成。
- 审查重点：生产请求不能把 HMAC secret 放入浏览器；FILE_SHARE HMAC-only 路由若浏览器无签名应明确显示 unauthorized，不能伪造 healthy；检查 Console 是否只调用公开 Gateway；检查新增 `pnpm-workspace.yaml` 是否确属必要且不能替代根锁文件。

## 6. 尚未开始或尚未完成的工作

### 尚未开始

- W5：联调材料和最终验收报告。
- Lead 对 W1-W4 的逐 commit 审查、冲突解决和统一集成。
- 集成后的全量 Contract/P0/Enterprise/Console 回归。
- 真实或明确标注的本地服务 preflight、FILE_SHARE E2E-01、formal v2 E2E-02。
- Postman Collection/Environment 与最终代码、错误码、状态字段的一致性校对。
- 最终设备对接协议、非敏感验收 artifact 和 deferred/blocked 清单的封版。

### 部分完成但不能宣称通过

- 主链当前只具备历史基线和已提交契约/Stub 修订，没有本轮 W1/W2 Gateway runtime 集成证据。
- v2.1.0 Attachment 契约已冻结，但 W3 runtime 未合并；当前主工作树不能依据该契约宣称 Attachment 可运行。
- 一对一 Stub/fixture 已提交，但 W2 formal query runtime 未合并。
- Console 代码在独立 worktree 中，但没有可采信的 TypeScript/Vitest/build 结果。
- 已有定向测试通过只代表各 worktree 某个中间现场，不代表当前 HEAD 的集成结果。

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

- 主分支和四个 worktree 基于不同的提交状态；直接合并可能产生契约、测试 fixture 或调用边界冲突，必须由 Lead 审查后集成。
- W1/W2/W3/W4 都没有形成可直接 cherry-pick 的最终 commit；worktree 中的未提交修改是恢复材料，不能当作已经纳入 Git 历史。
- 当前主分支的 Attachment route runtime 仍可能是旧的隐藏/501 行为，直到 W3 被审查并合并；契约和实现暂时不一致是已知风险。
- 当前主分支的 formal Gateway 仍未接入 W2 严格 canonical identity；已提交的 Stub 约束不能替代 runtime 约束。
- 固定 profile 的 readback 硬门仍未进入主分支；真实 RAGFlow parser/chunk/index/retrieval 尚未在本轮集成 commit 上验证。
- Console 的前端依赖环境未准备好；不能把静态源码存在当作 build 通过。
- 尚未执行本轮集成后的真实 FILE_SHARE、RAGFlow、Redis/Valkey、Enterprise DB、Asset Registry、对象存储和正式 JWT/HMAC 联调；外部依赖缺失时必须记录 blocked，不能伪造 pass。
- `output/`、`tmp/` 存在不可提交运行产物，并且部分路径权限可能导致递归枚举失败；不要为生成 checkpoint 而删除或移动它们。
- 仓库里还有多个历史 worktree/分支（M1/M3/T3 等），不属于本轮 W1-W4；除非新的任务明确要求，不要清理或改写它们。
- GitHub/PR 发布尚未执行；后续推送前需要重新确认远端认证和待推送 commit 范围，但本 checkpoint 不授权 push。

## 9. 测试证据和未执行测试

### 本轮已有证据

- v2.1.0 contract static/profile：此前 23 passed。
- W2B Asset Registry Stub：8 passed；Python compile 检查通过。
- W1 固定 profile：143 项定向回归通过；`compileall` 与 `git diff --check` 通过，但尚未 commit/集成。
- W2 formal query：39 项定向回归通过；新增独立 Registry 边界测试在暂停时尚未完成最终运行。
- W3 Attachment：32 项定向回归通过；`git diff --check` 通过，但 session/FILE_SHARE 回归尚未完成。
- 在本轮 W1-W4 修改前，曾有旧基线 Enterprise/backend 与 web 测试通过记录；这些历史结果不能替代当前 HEAD 集成回归。

### 尚未执行或当前不能采信

- W1-W4 合并到当前 HEAD 后的统一 pytest/compileall/diff check。
- W2 新增 `test_w2_asset_registry.py` 的最终运行结果。
- W3 的会话和 FILE_SHARE 相关回归。
- W4 TypeScript、Vitest、build；当前缺少项目 `node_modules`。
- 当前修改集上的完整 Contract/P0 profile。
- 本阶段真实 preflight、E2E-01 文件入库/解析/检索、E2E-02 问询/重启恢复/权限隔离。
- 最终 Postman/Newman smoke、secret scan、upstream change guard 和验收报告。

## 10. 恢复后的推荐执行顺序

1. 先读取本文件、`AGENTS.md`、`docs/设备管理系统联调优先分阶段实施计划.md` 和 v2.1.0 契约；确认仍在 `codex/device-integration-plan`，不要切到 master。
2. 对 W1、W2、W3、W4 分别执行 `git status`、`git diff --check`、定向测试和 diff 范围审查；不要先修改代码。
3. 先完成 W1/W2/W3/W4 各自最终 self-review 和单一聚焦 commit。W1/W2/W3 属后端，W4 属 web；遇到跨范围需求先停下。
4. Lead 按依赖集成：先 W1/W2/W3 的后端运行时，再 W4 Console；每次只集成一个可回滚 commit，并处理基线 `501dde7` 与当前 `d306e11` 的差异。
5. 集成后运行契约静态测试、W1/W2/W3 定向测试、既有 FILE_SHARE/formal/session 回归、compileall 和 `git diff --check`；失败必须修复或明确 blocked，不能删测试。
6. 重新核对 OpenAPI v2.1.0 与运行时 route visibility、auth、response/error envelope、`retryable` 和字段命名。
7. 运行 W4 的 TypeScript/Vitest/build；若依赖缺失，只记录环境 blocked，不生成或提交新的 lock 文件。
8. 再执行 preflight；区分 configured/healthy/unavailable/unauthorized。只有真实依赖可用时才执行 E2E-01/E2E-02；否则保留 blocked 证据。
9. 最后由 W5/Lead 更新 Postman、runbook、验收报告和 deferred 项；检查无 secret、无无效 artifact，确认 Console 不阻塞后端。
10. 仅在 Lead 统一审核和所有必要 Gate 结果可解释后，再讨论是否提交本 checkpoint、推送分支或创建 PR。本文件当前不授权这些操作。

## 11. 恢复时的安全边界

- 不读取、复制或写入 `.env`、API Key、Token、密码、Cookie、JWT、HMAC secret 或其他敏感值到本文件、日志、测试 artifact 或 Git。
- 不执行 `git reset --hard`、`git checkout --`、批量删除或递归清理；四个 worktree 的未提交修改必须保留，除非用户另行明确授权。
- 不把 `output/`、`tmp/`、PDF/CSV/SQL/日志等测试产物加入提交；只提交源码、测试、契约和必要文档。
- 任何需要修改主契约、数据库 migration、RAGFlow upstream、根锁文件、Compose 或认证模型的请求，先停止并报告决策点。
