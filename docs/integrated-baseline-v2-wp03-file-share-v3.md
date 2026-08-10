# Integrated baseline：v2、WP-03 与 file-share-v3

## 基线身份

- Baseline ID：`tyrag-integrated-candidate-v2-wp03-file-share-v3`
- Parent candidate：`34c6660d52dc25dfd77af4ad37270b44825495c8`
- RAGFlow：`v0.26.4` / `cb93883f3f8c975eecb2fed81210effeb3bdb06f`
- 状态：`INTEGRATED_CANDIDATE_BASELINE`，不是生产验收。

本记录是工作包边界、上游补丁证据和验收结果的索引。所有路径均以当前仓库根目录为准；共享文件的最终收口由 Lead 完成。

## 纳入矩阵

| 工作流 | 设计/契约证据 | 实现范围 | 独立测试证据 | 纳入结论 |
|---|---|---|---|---|
| External v2 | `decisions/ADR-006-External-Integration-v2-并行基线.md`、`contracts/integration-openapi-v2.yaml`、`tasks/P0-External-Integration-Contract-Rebaseline-v2.md` | `enterprise/gateway/auth`、`enterprise/gateway/query`、`enterprise/gateway/sync` 及 v2 测试 | Contract 18；P0 离线 416；TypeScript noEmit；Vitest 84 | 纳入候选基线 |
| WP-03 | `tasks/WP-03-解析与质量门禁.md`、`docs/WP03-effective-parsing-audit.md` | Enterprise parser application、质量门和 WP-03 验收脚本/测试 | WP03 离线 72；正式样本门禁保持 BLOCKED | 纳入候选基线，不能宣称 PROVEN |
| file-share-v3 | `decisions/ADR-007-外部文件权威源与零持久PDF.md`、`patches/CHANGE-REQUEST-外部文件票据解析.md`、`contracts/file-share-v3.yaml` | Gateway FILE_SHARE catalog/ticket/source access、v3 router、RAGFlow client/worker 适配 | `enterprise/tests/test_external_file_source.py` 8；上游入口静态契约测试；`py_compile` | 作为 RF-PATCH-002 可重放候选纳入 |

## RAGFlow 上游补丁审查

只有下列 4 个 `ragflow/**` 文件属于本基线的 `RF-PATCH-002`：

- `ragflow/api/apps/restful_apis/document_api.py`
- `ragflow/rag/utils/external_source.py`
- `ragflow/rag/svr/task_executor.py`
- `ragflow/rag/svr/task_executor_refactor/task_handler.py`

每个文件均由同一 ADR、Change Request 和独立测试覆盖；测试同时保护虚拟文档注册/换票入口、两个 task executor 的 `external://` 分支、票据哈希校验和临时文件读取。补丁不修改官方迁移、官方对象存储抽象或文档引擎，并在 `patches/manifest.yaml` 中登记了回滚与升级定位。

## 共享文件收口

- `enterprise/gateway/app.py`：Lead 收口 v2、v3 document router 和 internal source-ticket router，各路由只装配一次。
- `contracts/integration-openapi-v2.yaml`：沿用已冻结 v2 wire contract；本轮不做未授权 additive 变更。
- `contracts/file-share-v3.yaml`：Lead 收口 FILE_SHARE 请求、一次性 ticket 和 strict schema；独立测试校验路径与关键 schema。
- `patches/manifest.yaml`：Lead 收口 `RF-PATCH-002`，记录 ADR、Change Request、上游文件、测试和可重放边界。

`contracts/wp04-phase3-contract-freeze.md` 不属于本次 v2/WP-03/file-share-v3 集成范围，保持在工作区外置，不能随本基线纳入主链。

## 验收结论

- `git diff --check`：通过。
- `Contract`：通过，18 tests，0 failures/skips。
- `P0`：通过，Python 416 tests；TypeScript noEmit 通过；Vitest 84 tests。
- `WP03`：离线 72 tests 通过；正式验收因 `artifacts/wp03/real-acceptance/manifest.json` 缺失返回 exit 2，符合 WP-03 的 BLOCKED 规则，不以合成样本替代。
- file-share 独立测试：8 tests 通过。
- RAGFlow 补丁语法检查：4 个文件 `python -m py_compile` 通过。

Runner evidence：

- Contract summary：`C:\CodingProgram\WAES\TYrag\artifacts\enterprise-tests\20260810T063911Z-40716\summary.json`
- P0 summary：`C:\CodingProgram\WAES\TYrag\artifacts\enterprise-tests\20260810T063934Z-45016\summary.json`
- WP03 summary：`C:\CodingProgram\WAES\TYrag\artifacts\enterprise-tests\20260810T063923Z-33144\summary.json`

## 后续集成边界

生产接受仍需真实 S1–S8 脱敏样本/manifest、RAGFlow/对象存储环境、Asset Registry 和 Redis/Valkey 跨实例证据。任何上游升级必须按 `RF-PATCH-002` 逐文件重放并重跑 Contract、P0、WP-03 与 file-share 独立测试。
