# Agent 交接：Gateway PostgreSQL 全量切换 — 测试与 30 机部署前检查

面向：接手本工作包后续验证 / 本机 Docker 联调 / 打到 30 联调机的 Agent。  
来源对话：SQLAlchemy Core 替换 Gateway「Repository Protocol + 自建 UoW」方案收口，并完成独立 Gateway PostgreSQL 切换（约 2026-08-30）。  
触发语：「按 sqlalchemy-core handoff 做本地验证」「打 30 前检查」「SQLAlchemy 迁移回归」。

未接到用户明确「更新到 30」时，**不要**部署 30。离线门禁或本机冒烟未绿时，**不要**部署 30。

---

## 0. 开工前必输出

- 成功标准：见下文「验收分层」；本任务默认只做到用户指定的那一层。
- 将读取：本文件、[`update-30-server-agent.md`](update-30-server-agent.md)、`enterprise/gateway/db/`、相关测试与 `enterprise/scripts/run_enterprise_tests.ps1`。
- 将修改：仅用户明确授权的范围；部署 30 时按 update-30 文档改 30 上 overlay / 镜像，**不改**仓库 `production.env.example` 默认 bind。
- 契约版本：EAM v2 / OpenAPI 基线不变（本迁移不改对外契约）。
- 不会修改：RAGFlow 官方迁移、主 OpenAPI、根锁文件、客户数据；不在日志/chat 回显 password / API key / Token。
- 验证命令：见 §3；Windows 下 pytest 建议带仓库内 `--basetemp`。
- 主要风险：把「离线 P0 绿」误当成「本机 Docker / 30 已验收」；PG 凭据或数据迁移未核对；recreate 后端口绑回 `127.0.0.1`。

---

## 1. 本次改了什么（事实基线）

### 1.1 目标（已实现范围）

- 用 **SQLAlchemy Core** 作为 Gateway 数据库 provider / 方言 / 连接 / 事务层。
- 新增薄封装 [`enterprise/gateway/db/`](../../enterprise/gateway/db/)：`GatewayDatabase`、`dialect`、`schema`/`tables`、`ops`（`gw_read`/`gw_write`）、`exceptions`、`testing.create_gateway`。
- Store 继续承载业务数据访问；入参为 **`AsyncConnection`**，由 `gateway.transaction(...)` 提供。
- Router / service / worker **不**直接 `commit`；网络 / RAGFlow / 对象存储调用 **不**放在 DB 事务内。
- **已切换 PostgreSQL**（`postgresql+asyncpg`）。SQLite 只保留在一次性只读迁移工具中；Gateway 运行时、开发夹具和测试均走 PG。

### 1.2 明确不做 / 未验收

| 项 | 状态 |
|---|---|
| PostgreSQL 作为 Gateway 状态库 | **已实现**；使用独立 `gateway-postgres` 与 `ENTERPRISE_GATEWAY_DB_*` |
| 本机 Docker 真实 HTTP / 全链路 E2E | **Gateway 镜像+PG health 已通过**；完整 RAGFlow/FILE_SHARE live E2E 未验收 |
| 部署到 `192.168.30.30` | **未做**；需用户明确授权 + 网络可达 |
| 把 `parser_application_status` 重新绑回 retrieval readiness 门禁 | **有意不回退**；`parser_readback` 只看 `pipeline_status in {DONE,3}` |

### 1.3 当前验证结论（2026-08-30）

本次恢复后已在开发机临时 PostgreSQL 上复跑；30 机仍未部署：

| 门禁 | 结果 |
|---|---|
| PG 迁移 / schema / claim 并发 / 架构边界 | 9 passed |
| `run_enterprise_tests.ps1 -Profile P0` | PASS：pytest-offline 817；v2-inquiry-smoke；tsc；vitest 134 |
| `test_enterprise_runner.py` | 17 passed |
| 重建 Gateway 镜像 + PG HTTP health | HTTP 200；镜像内迁移工具可执行 |
| `192.168.30.30` | ICMP/SSH 仍不可达；未部署 |

后续若代码或环境再变更，仍需按 §3 复跑，不要只引用本表。

### 1.4 仓库工作区状态（部署前必查）

- 大量改动可能仍在 **本地未提交**（`master` 上 `M`/`??`，含整个 `enterprise/gateway/db/`）。
- 计划内一次性脚本已删（如 `_bulk_fix_init_db.py`、`_replace_init_db.py`、`_migrate_models.py`、`_fix_test_wp02b*`）。
- **仍可能残留**其它 `_migrate_*` / `_patch_*` / `migrate_store_to_sqlalchemy.py`（勿当运行时依赖；可清理但勿误跑）。
- 若干 `.pytest_tmp*` / `enterprise/.pytest_tmp*` 为测试垃圾，**不要**提交、**不要**同步到 30。
- 若本机旧的 `docker-enterprise-gateway-1` 因遗留 `ENTERPRISE_*_DB_PATH` 持续重启，不代表新镜像失败；清理旧 env 后按 §3.4 用 PG 配置 recreate。

---

## 2. 验收分层（交给其它 Agent 时先选定层级）

```text
L0  静态/离线回归     →  最低合并/继续开发门槛
L1  本机 Docker 冒烟  →  打 30 前强烈建议
L2  打 30 + 局域网门禁 →  仅用户明确「更新到 30」后
L3  联调业务验收      →  EAM/设备侧场景；本 handoff 只列清单
```

**PostgreSQL：** 本工作包已覆盖方言、DDL、claim/lease、数据迁移和契约测试。30 机切换仍需按 [`update-30-server-agent.md`](update-30-server-agent.md) 做备份、短停机迁移和局域网验证。

---

## 3. 怎么测（L0 / L1）

### 3.1 环境注意（Windows）

默认 `%TEMP%\pytest-of-*` 曾出现 **WinError 5 PermissionError**。统一使用仓库内 basetemp，例如：

```powershell
$Basetemp = "enterprise/.pytest_tmp_handoff_$(Get-Date -Format yyyyMMddHHmmss)"
```

### 3.2 L0 — 离线回归（必做）

在仓库根目录：

```powershell
# A. 本工作包焦点套件
python -m pytest `
  enterprise/tests/test_file_share_v3_status.py `
  enterprise/tests/test_wp03_phase2.py `
  enterprise/tests/test_demo_loop.py `
  enterprise/tests/test_formal_query.py `
  enterprise/tests/test_transient_attachment.py `
  -q --tb=line --basetemp=$Basetemp

python -m pytest `
  enterprise/tests/test_gateway_db.py `
  enterprise/tests/test_db_architecture_boundary.py `
  enterprise/tests/test_wp02a.py `
  enterprise/tests/test_wp02b.py `
  enterprise/tests/test_callback_delivery.py `
  enterprise/tests/test_v2_document_contract.py `
  enterprise/tests/test_ragflow_delete_sync.py `
  enterprise/tests/test_m3e_historical_import.py `
  -q --tb=line --basetemp="${Basetemp}_b"

# B. P0 profile（含 smoke / tsc / vitest）
pwsh -File enterprise/scripts/run_enterprise_tests.ps1 -Profile P0
```

**架构边界硬断言**（B 中已含，也可单跑）：

- `enterprise/tests/test_db_architecture_boundary.py`：Gateway 运行代码不得 `import sqlite3` / `aiosqlite`；SQLite 仅允许出现在一次性迁移脚本及其专门测试。

**关注语义变更（勿按旧断言「修回产品」）：**

- Readiness：`parser_readback` ≠ `parser_application_status == executed`。
- 部分测试在 `TEST_TENANT_OPEN` 下用 `business_status` 等表达 ACL，而不是假想的 group deny。

### 3.3 L0 失败时优先排查

| 症状 | 优先检查 |
|---|---|
| `GatewayDatabase` 无 `execute` / `exec_driver_sql` | 调用方把 gateway 当成 connection；应 `transaction` + `exec_sql`/`fetchone` 或 `gw_read`/`gw_write` |
| `NameError: conn` / `async with conn.execute` | 服务仍半迁移；参考 `transient_attachment.py`、`citation_file.py` |
| `ValueError: too many values to unpack` | fixture 解包：`isolated_gateway_db` → `(gateway, db_path)` |
| 期望 `PARSER_READBACK_NOT_READY` / `parser_application_status` | 测试过时；对齐 [`readiness.py`](../../enterprise/gateway/sync/readiness.py) |
| Windows PermissionError on temp | 换新鲜 `--basetemp`，勿复用被锁住的旧目录 |

### 3.4 L1 — 本机 Docker 冒烟（打 30 前建议）

先启动本地测试 PostgreSQL（默认 `127.0.0.1:55432`），再启动 Gateway Compose；不要把业务数据库或 30 机当作开发测试库。若 Docker Desktop 不可用，记录为 L1 阻塞。

```powershell
# 先准备未提交的本地测试 env（包含 DB/JWT/HMAC 等必填值）；RAGFlow base
# 必须放在 overlay 前，才能提供 ragflow 网络和服务名。
docker compose -f ragflow/docker/docker-compose.yml `
  -f deploy/overlays/docker-compose.enterprise.yml `
  --profile cpu up -d gateway-postgres enterprise-gateway
docker compose -f ragflow/docker/docker-compose.yml `
  -f deploy/overlays/docker-compose.enterprise.yml ps
```

访问（以本机实际 compose 为准）：Gateway `http://localhost:5188`；完整 RAGFlow 栈仍按 [`AGENTS.md`](../../AGENTS.md) 启动。

**建议冒烟清单（不替代完整 E2E）：**

1. `gateway-postgres` healthy 后 Gateway 进程能起来；lifespan 初始化 `GatewayDatabase`，关闭时 `dispose()`。
2. 配置 `ENTERPRISE_GATEWAY_DATABASE_URL`，或 `ENTERPRISE_GATEWAY_DB_HOST/PORT/NAME/USER/PASSWORD`；旧 `ENTERPRISE_*_DB_PATH` 在生产启动时应 **fail fast**。
3. 选 1 条 sync / v2 inquiry / citation / transient attachment 只读或幂等写路径，确认无 `AttributeError` / 500。
4. Worker（sync / quality / callback）领取租约：确认使用 `gateway.transaction(write=True)`，无跨 task 共享 connection。
5. **不要**在事务内打 RAGFlow HTTP（已知风险点：`promote_quality_passed_version` 曾因此挂起；应保持 API 在事务外）。

本地真实 HTTP E2E 若受 embedding DNS 等环境阻塞：**分开报告**，不得用离线绿冒充 live 绿。

### 3.5 依赖与运行时

- 运行时：`enterprise/requirements-runtime.txt` 固定 `SQLAlchemy==2.0.45` 与 `asyncpg==0.31.0`；不安装 `aiosqlite`。
- 测试：`SQLAlchemy>=2.0.45,<2.1.0`。
- 镜像若缺依赖：需按 AGENTS.md **重建** enterprise/ragflow 相关镜像，不能假设基础镜像已装。

---

## 4. 部署到 30 前：检查与处理清单

完整部署步骤以 [`update-30-server-agent.md`](update-30-server-agent.md) 为准。下面是 **本迁移特有** 的附加项。

### 4.1 代码与制品

- [ ] L0 复跑通过；建议 L1 冒烟通过或书面记录阻塞原因。
- [ ] 确认要部署的提交 / 工作树内容包含 `enterprise/gateway/db/` 及所有已改 gateway/tests/scripts。
- [ ] **不要**把 `.pytest_tmp*`、`artifacts/`、一次性 `_migrate_*` / `_patch_*` 脚本同步为运行依赖。
- [ ] 确认 `requirements-runtime.txt` 中的 SQLAlchemy / asyncpg 会进入 30 上 Gateway 镜像构建上下文，并包含 `migrate_gateway_sqlite_to_postgres.py`。
- [ ] 上游 RAGFlow：**本迁移不应改上游**；若工作树混有无关上游改动，部署前剥离。

### 4.2 配置与数据（PostgreSQL）

- [ ] 30 上准备独立 Compose 服务 `gateway-postgres` 与持久卷；Gateway 只读写该库。
- [ ] 迁移前备份现网 SQLite 文件，并确认 Gateway 已停止，避免迁移期间继续写入。
- [ ] 使用镜像内 `migrate_gateway_sqlite_to_postgres.py`，SQLite 源只读、目标库必须为空；保存不含正文/secret 的 manifest。
- [ ] 迁移完成后核对每表行数和摘要，才 recreate Gateway；旧 SQLite 仅作为回滚副本。
- [ ] schema 由 `initialize_schema` 版本化（当前 v2）；不要在 30 上手工改表。

### 4.3 安全

- [ ] 不回显、不写入 chat / 日志 / 提交物：password、API key、Token、Cookie、JWT。
- [ ] 不把 secret 写进仓库或 example env 默认值。
- [ ] 客户数据不得拉取到开发机做测试。

### 4.4 30 部署硬门禁（摘自 update-30，必须执行）

- [ ] 网络可达 `192.168.30.30`；SSH 按 update-30 文档。
- [ ] **禁止** `--env-file production.env.example` 做 recreate。
- [ ] recreate 使用现网 bind 为 `0.0.0.0` 的 env 组合（含 `gateway-overrides.env` 等，见 update-30）。
- [ ] recreate 后从 **开发机** 验证局域网端口，而不是只在 30 上 `curl 127.0.0.1` 或看 healthy：
  - `http://192.168.30.30:8080/`
  - `http://192.168.30.30:9380/api/v1/system/ping` → `pong`
  - Gateway `5188`、enterprise-web `3000`
- [ ] `ss -lntp` 对应端口 HostIp 仍为 `0.0.0.0`；若变 `127.0.0.1`：**先修 bind，再做功能验证**。
- [ ] compose 使用 `--no-deps`、`--pull never`；不重建 mysql/es/minio/redis。
- [ ] 部署后在容器内 grep 本次 marker（例如 `GatewayDatabase`、`enterprise.gateway.db`）证明新代码在镜像/挂载中。

### 4.5 打 30 后功能抽检（L2/L3 最小集）

在局域网可访问的前提下：

1. Gateway health / 就绪。
2. 一条只读或低风险 v2/formal 查询路径（注意 ACL；勿打真实客户文档）。
3. Citation / attachment 若环境允许：确认无半迁移 500。
4. Sync / callback / quality worker 无持续报错刷屏（查容器日志，**脱敏**）。
5. 消息业务状态与 `citations` **解耦**规则仍成立（见 AGENTS.md §9）：不得用 citation 空否改判 `completed` / `no_reliable_evidence` / `failed`。

---

## 5. 已知残留风险（接手时知情）

1. **P0 外** 仍可能有「把 `GatewayDatabase` 当 Connection」的漏网调用；遇 `AttributeError` 按 §3.3 修。
2. 本机 Docker Desktop 当前可能未运行；没有 PG 集成证据时，不得宣称 L1 或 30 验收。
3. `TEST_TENANT_OPEN` 测试语义与真实 group ACL 产品行为不完全等价。
4. 一次性迁移脚本必须对空目标执行；重复执行会被拒绝，误用生产目标仍有数据风险。
5. 文档 [`12-测试评测与验收标准.md`](../12-测试评测与验收标准.md) / WP03 审计里可能仍写旧的 SQLite 直连方式——以代码与本 handoff 为准，旧文档未全部改写。

---

## 6. 完成报告模板（给交接 Agent）

结束时报告：

- 做到哪一层（L0/L1/L2/L3）
- 复跑命令与 pass/fail 计数（附 artifact 路径若有）
- 修改文件列表（若有热修）
- 是否修改上游（应为否）
- 是否部署 30（是/否）；若是：bind 局域网验证证据
- 未解决问题与风险
- **不声明** live E2E / 生产验收，除非用户要求的层级已真实做完

---

## 7. 相关路径速查

| 路径 | 用途 |
|---|---|
| `enterprise/gateway/db/` | GatewayDatabase 基础设施 |
| `enterprise/gateway/db/ops.py` | `gw_read` / `gw_write` |
| `enterprise/gateway/sync/readiness.py` | retrieval readiness（勿误回退 parser_application 门禁） |
| `enterprise/tests/test_gateway_db.py` | 数据层单测 |
| `enterprise/tests/test_db_architecture_boundary.py` | 架构边界 |
| `enterprise/scripts/run_enterprise_tests.ps1` | P0 等 profile |
| `docs/integration/update-30-server-agent.md` | 30 机部署权威步骤 |
