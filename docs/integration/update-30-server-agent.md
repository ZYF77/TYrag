# Agent 任务：本地测通后更新部署到 30 服务器

面向：负责把已测通改动打到联调机 **192.168.30.30** 的 Agent。  
触发语：「更新部署到30」「按上次方式打到 30」「把当前实现同步到 30」。  
联调机角色：EAM（31）打 Gateway、人从局域网打开 8080/3000 的 **实验室主机**，不是高可用生产。

未接到用户明确「更新到 30」时，不要部署。本地测试未绿时，不要部署。

---

## 0. 开工前输出

- 成功标准：目标服务健康 **且** 局域网能打开对应端口；本次改动的 marker 在线上可见。
- 将读取：本文件、`deploy/production/docker-compose.yml`、本地已改文件。
- 将修改：30 上 overlay 源码目录和对应 Docker 镜像/容器；**不改**仓库里的 `production.env.example` 默认值。
- 契约版本：沿用当前工作包；本任务不改 OpenAPI。
- 不会修改：RAGFlow 官方迁移、主 OpenAPI、根锁文件、客户数据、`.env` 里的 secret。
- 验证：局域网 `http://192.168.30.30:<port>`，不是只在 30 本机 `127.0.0.1` 上 curl。
- 主要风险：`docker compose ... --force-recreate` 把端口绑回 `127.0.0.1`，容器内部仍 healthy，外部全挂。

---

## 1. 硬门禁：外部访问

仓库 `deploy/production/docker-compose.yml` 和 `production.env.example` 默认：

```
RAGFLOW_BIND_ADDRESS=127.0.0.1
ENTERPRISE_GATEWAY_BIND_ADDRESS=127.0.0.1
ENTERPRISE_WEB_BIND_ADDRESS=127.0.0.1
```

30 联调 **必须** 听 `0.0.0.0`，否则 EAM（31）和开发机都打不开。

已经发生过多次：更新后 30 上 `curl 127.0.0.1:8080` 通、health=healthy，局域网 `http://192.168.30.30:8080` 打不开。根因是 recreate 用了 compose 默认或 `production.env.example`。

### 禁止

- 用 `--env-file production.env.example` 做 recreate（会把 bind 打回 loopback）。
- 只凭 30 本机 `127.0.0.1` 或容器内 health 宣布成功。
- `docker compose up` 不带 `--no-deps`（会连带重建依赖）。
- 不带 `--pull never`（30 常拉不到外网基础镜像）。
- 重建 `mysql` / `es01` / `minio` / `redis`。
- 回显、复制、写入 chat 的 password / secret / API key / Token / JWT。
- 为「方便」改 bind 去改仓库里的 example 默认值。

### 每次 recreate 前后必做

**前**：记下当前 HostIp。

```bash
ss -lntp | grep -E ':8080|:9380|:5188|:3000'
```

当前健康态应为 `0.0.0.0:8080`、`0.0.0.0:9380`、`0.0.0.0:5188`、`0.0.0.0:3000`。

**后**：同一条 `ss` 仍必须是 `0.0.0.0`。再从 **开发机**（不是 SSH 进 30）访问：

| 服务 | 开发机探测 |
|---|---|
| RAGFlow Web | `http://192.168.30.30:8080/` HTTP 200 |
| RAGFlow API | `http://192.168.30.30:9380/api/v1/system/ping` 正文 `pong` |
| Gateway | `http://192.168.30.30:5188/` 或 `/health` 能通（不要把 JWT 打进日志） |
| enterprise-web | `http://192.168.30.30:3000/` HTTP 200 |

任一端口变成 `127.0.0.1`：**先修 bind，再继续功能验证**。不要等用户说「又访问不到」。

### 现网 bind 陷阱（2026-08-19 实测）

| 文件 | 现状 |
|---|---|
| `/home/zkadmin/tyrag-production/.env` | `RAGFLOW_BIND_ADDRESS=0.0.0.0`，`ENTERPRISE_GATEWAY_BIND_ADDRESS=0.0.0.0`，**`ENTERPRISE_WEB_BIND_ADDRESS=127.0.0.1`** |
| `gateway-overrides.env` | Gateway / Web 均为 `0.0.0.0` |
| `core.env` | `RAGFLOW_BIND_ADDRESS=0.0.0.0` |
| `dev-ragflow.env` | `API_PROXY_SCHEME=python`（缺了 9380 起不来） |
| `production.env.example` | 全部 `127.0.0.1`，**禁止拿来 recreate** |

只 `cd tyrag-production && docker compose ... enterprise-web` 会读 `.env`，把 **3000 打回本机**。Web 必须带上 `gateway-overrides.env`。

RAGFlow 若只 recreate、不加载 `API_PROXY_SCHEME=python`，8080 可能还在、9380 起不来，Gateway 会变 unhealthy。不要依赖 `/tmp/ragflow-bind-override.yml`（曾经写过、不持久）。

---

## 2. 环境清单

| 项 | 值 |
|---|---|
| 主机 | `192.168.30.30`（hostname `vectorserver`） |
| SSH | `ssh -i C:\Users\Lemon\.ssh\tyrag_zkadmin -o BatchMode=yes zkadmin@192.168.30.30` |
| Compose 目录 | `/home/zkadmin/tyrag-production` |
| Gateway overlay 源 | `/home/zkadmin/tyrag-src/gateway-build` |
| 项目名 | `tyrag-production` |
| 本地 Docker | 开发机经常 **没有** Docker；镜像在 30 上 build |

| 容器 | 镜像 tag | 端口 |
|---|---|---|
| `tyrag-production-enterprise-gateway-1` | `tyrag/enterprise-gateway:v0.26.4` | 5188 |
| `tyrag-production-ragflow-cpu-1` | `tyrag/ragflow:v0.26.4`（历史 overlay 也可能是 `v0.26.4-tyrag-053807f`） | 8080 / 9380 |
| `tyrag-production-enterprise-web-1` | `tyrag/enterprise-web:v0.26.4` | 3000（diagnostics） |

Gateway 镜像 **只读 rootfs**，改 Python **不能** 只 restart。必须 overlay 打进镜像再 recreate **这一个** 服务。

Windows 上传的脚本先 `sed -i 's/\r$//'`。

---

## 3. 先判断打哪一层

| 本地改动 | 30 上做法 |
|---|---|
| `enterprise/gateway/**`（**无** schema/迁移） | 小 overlay 打进现网 gateway tag，只 recreate `enterprise-gateway` |
| `enterprise/gateway/**`（**含** `SCHEMA_VERSION` / `initialize_schema` / 新表） | **优先正式 gateway 镜像**（含完整 schema 代码）；**不要**只 overlay 几个业务 .py 却漏掉 `db/schema.py`；仍只 recreate `enterprise-gateway`，迁移由启动时 `initialize_schema` 完成 |
| `enterprise/web/**` | 源码打到 30 构建 `tyrag/enterprise-web:v0.26.4`，只 recreate `enterprise-web` |
| `ragflow/**`（Python/API/RAG/Go 等镜像内代码） | 必须把当前完整源码构建成新的 RAGFlow 镜像并导入，随后只 recreate `ragflow-cpu`；不能只 restart 或只更新 Gateway |
| `ragflow/web/**`（8080 UI） | **优先** 把 `web/dist` `docker cp` 进正在跑的 RAGFlow + `nginx reload`，**不要** recreate |
| 仅配置 / 未改代码 | 不要发版 |

一次只动改动对应的服务。设备身份治理（§5B）仅需 Gateway；仅当本次 diff 确实改了 `ragflow/**` 才更新 `ragflow-cpu`。

30 现有 `docker-compose.yml` 不得被发布包整体覆盖。使用包内的
`docker-compose.30-release.yml` 作为叠加文件；它只增加 Gateway PostgreSQL、切换三个镜像并
清空旧 SQLite 路径，保留 30 原有的外部附件卷、外部源回调、Asset Resolver 和其他服务配置。
发布包内的 `release.env` 只含镜像 tag 和非敏感开关，不替代 30 的 `.env`。

---

## 4. Gateway PostgreSQL 切换（本工作包）

本次 Gateway 状态库从现网 SQLite 切到 Compose 管理的独立 PostgreSQL。
这是一次短停机操作；在 30 未连通前只准备，不执行下面命令。

1. 在 30 上确认现网 Gateway 已停止前，先复制并校验 SQLite 备份（保留原文件，供回滚）。
2. 先只启动数据库并等待 healthy：

   ```bash
   cd /home/zkadmin/tyrag-production
   docker compose --env-file .env --env-file gateway-overrides.env \
     --env-file /tmp/offline-release-<commit>/release.env \
     -f docker-compose.yml -f docker-compose.30-release.yml \
     up -d --no-deps --pull never gateway-postgres
   docker compose --env-file .env --env-file gateway-overrides.env \
     --env-file /tmp/offline-release-<commit>/release.env \
     -f docker-compose.yml -f docker-compose.30-release.yml ps gateway-postgres
   ```

3. 用新 Gateway 镜像执行一次性迁移。迁移工具只读 SQLite、拒绝非空 PG，且不会把正文或 secret 写入 manifest：

   ```bash
   docker compose --env-file .env --env-file gateway-overrides.env \
     --env-file /tmp/offline-release-<commit>/release.env \
     -f docker-compose.yml -f docker-compose.30-release.yml run --rm --no-deps \
     -e ENTERPRISE_DB_PATH= -e ENTERPRISE_SYNC_DB_PATH= \
     --entrypoint /ragflow/.venv/bin/python enterprise-gateway \
     /ragflow/enterprise/scripts/migrate_gateway_sqlite_to_postgres.py \
     --sqlite-path /var/lib/tyrag/state/gateway.db \
     --manifest /var/lib/tyrag/state/gateway-postgres-migration.json
   ```

4. 核对迁移输出的每表行数和摘要；从 30 的 `.env` / env-file 中删除旧 `ENTERPRISE_DB_PATH`、`ENTERPRISE_SYNC_DB_PATH` 后，按下一节构建/覆盖 Gateway 镜像并 recreate。迁移失败时保留 SQLite 原文件，不启动新 Gateway。
5. 回滚顺序：停止新 Gateway，恢复原 Gateway 镜像和 SQLite 配置，再按同一 bind/env 组合 recreate；不要删除 PG 卷或 SQLite 备份。

`ENTERPRISE_GATEWAY_DB_NAME/USER/PASSWORD` 只放在 30 的权限受控 env/secret 中，禁止写进命令行、日志或仓库。PG 端口不发布到宿主机公网。

## 5. Gateway overlay（已测通的做法）

在开发机构包，不要把整个仓库 rsync 到 30。

```powershell
# 在仓库根。按实际改动的相对路径列文件。
tar -cf .pytest_tmp/gateway-overlay.tar -C enterprise/gateway query/v2_router.py query/formal_router.py
scp -i $env:USERPROFILE\.ssh\tyrag_zkadmin -o BatchMode=yes `
  .pytest_tmp/gateway-overlay.tar `
  zkadmin@192.168.30.30:/tmp/gateway-overlay.tar
```

30 上（先备份当前 tag，再覆盖同一 tag）：

```bash
set -euo pipefail
BUILD=/home/zkadmin/tyrag-src/gateway-build
PROD=/home/zkadmin/tyrag-production
TAG=tyrag/enterprise-gateway:v0.26.4
BACKUP=tyrag/enterprise-gateway:v0.26.4-before-<short-change-name>

docker tag "$TAG" "$BACKUP"
mkdir -p "$BUILD/gateway"
tar -xf /tmp/gateway-overlay.tar -C "$BUILD/gateway"
find "$BUILD/gateway" -type f -name '*.py' -exec sed -i 's/\r$//' {} +

cat > "$BUILD/Dockerfile.overlay" <<'EOF'
FROM tyrag/enterprise-gateway:v0.26.4-before-<short-change-name>
USER root
COPY --chown=tyrag:tyrag gateway/ /ragflow/enterprise/gateway/
USER tyrag
EOF

docker build -f "$BUILD/Dockerfile.overlay" -t "$TAG" "$BUILD"

cd "$PROD"
docker compose --env-file .env --env-file gateway-overrides.env \
  --env-file /tmp/offline-release-<commit>/release.env \
  -f docker-compose.yml -f docker-compose.30-release.yml \
  up -d --no-deps --force-recreate --pull never enterprise-gateway
```

`FROM` 必须是 **刚才打的 BACKUP tag**，不要 `FROM` 正在被覆盖的 `$TAG`（build 缓存会吃旧层）。

等 health=healthy 后：

1. `ss -lntp | grep 5188` → `0.0.0.0:5188`
2. 开发机访问 `http://192.168.30.30:5188`
3. `docker exec` 里 grep 本次 marker（函数名 / 常量），证明新代码在镜像里
4. 只跑与本次改动对应的冒烟；JWT 在容器内从环境变量签发，**禁止打印 token**

回滚：`docker tag $BACKUP $TAG`，同一条 compose recreate。

---

## 5B. 设备身份治理 / Gateway schema → v12（有迁移时）

适用：`ext_identifier_history`、`SCHEMA_VERSION=12`、INLINE_JSON 按设备单现行、v2 先解析身份再 `doc_ids`、prompt `current_identity` 注入。  
本变更 **含 Gateway PostgreSQL schema 升级**，**不算**「纯小 overlay」发版。

### 结论（给 Deploy）

- **可以 Gateway-only**：只换 / recreate `enterprise-gateway`，**不动** `ragflow-cpu` / `mysql` / `es01` / `redis` / `minio`，**禁止**整链 `up`、**禁止** recreate mysql、**禁止**删 `mysql_data` / `gateway_postgres_data`。
- 升级路径：新 Gateway 进程启动时跑现有 **`initialize_schema`**（v11→v12：建 `ext_identifier_history`、从 `ext_asset_registry` 回填 current 行、写 `gateway_schema_version=12`）。**不要**手工改表、不要为迁库重建 PG 容器。
- 镜像策略：优先 **正式 `tyrag/enterprise-gateway:<tag>`**（本机或构建机完整 build，含 `db/schema.py` / `tables.py` / `equipment_identity.py` 等）。仅当确认 **无 schema 变更** 时才用 §5 overlay；本次有 v12，漏打 schema 文件会导致启动拒迁或行为半新半旧。
- 工作区：只把身份治理相关文件打进镜像；**无关 dirty**（本机 `.env` 端口、本地 overlay、未相关 artifacts）**勿**打进发布镜像。

### 现网 Compose / env 组合（与迁卷文档一致）

在 `/home/zkadmin/tyrag-production`，GW 滚动常用：

```bash
cd /home/zkadmin/tyrag-production
docker compose \
  --env-file .env \
  --env-file gateway-overrides.env \
  -f docker-compose.yml \
  -f docker-compose.30-release.yml \
  -f docker-compose.gateway-pg-fix.yml \
  up -d --no-deps --force-recreate --pull never enterprise-gateway
```

说明：

- 双 env-file + 三 compose；缺 `docker-compose.30-release.yml` 会丢 console / 镜像 env；`gateway-pg-fix.yml` 为现网叠加（见 `deploy/production/docs/volume-migrate-to-data.md` §4.0）。
- 必须 `--no-deps --pull never`；**禁止**用 `production.env.example` recreate（会把 bind 打回 loopback，见 §1）。
- 默认不改 30 上 `.env` / 密码 / secret。

### 发版前核对（Probe / Deploy）

在 **gateway-postgres**（或 Gateway 容器经现有连接）只读查询，记录现网版本：

```sql
SELECT version, applied_at FROM gateway_schema_version ORDER BY version;
```

预期发版前多为 `11`（或文档登记的当前值）。若已是 `12`，先确认是否重复发版、表是否已存在，再决定是否仅换代码镜像。

同时记录当前 `enterprise-gateway` 镜像 tag / digest，便于回滚。

### 发版后核对（Deploy 探活 + 交 Sim）

1. 容器 `healthy`；`ss`：`0.0.0.0:5188`（及本次未动端口仍为 `0.0.0.0`）。
2. **开发机**访问 `http://192.168.30.30:5188`（health）与 console/`3000`（若未动 web，确认仍可登）。
3. Schema：

```sql
SELECT version FROM gateway_schema_version;  -- 应为 12
SELECT to_regclass('public.ext_identifier_history');  -- 非 null
SELECT COUNT(*) FROM ext_identifier_history WHERE status = 'current';
```

4. 容器内 marker：能看到 `SCHEMA_VERSION = 12`、`ext_identifier_history`、`current_identity` 相关符号（勿把 JWT/secret 打进日志）。
5. **业务验收交 Sim**（勿用哈希冒烟凑数）：身份历史/改号解析、同设备 INLINE_JSON 仅一条现行、回答侧 `current_identity` 注入；正式提问在 30 测，不在本机构造正式问答。

### 回滚

- 保留发版前 gateway 镜像 backup tag；同一 env/compose 组合只 recreate `enterprise-gateway`。
- Schema v12 为向前兼容加表/回填；回滚代码后表可残留。若必须回到仅识别 v11 的旧镜像，先确认旧 `initialize_schema` 是否拒绝「多表/高版本」；**仍禁止**删 PG 卷或 recreate mysql 来「清 schema」。


---

## 5A. RAGFlow Python/后端镜像（当前工作包）

生产 Compose 不挂载 RAGFlow 源码；`ragflow/**` 的任何 Python、API、RAG 或运行时代码改动，
必须进入新的 RAGFlow 镜像。不能只重启现有容器，也不能只更新 Gateway overlay。

在有 Docker 的构建机，从**干净的目标 Git commit**构建并记录唯一 release tag：

```bash
docker build --pull=false \
  -t tyrag/ragflow:<release-tag> \
  -f ragflow/Dockerfile ragflow

docker build --pull=false \
  --build-context contracts=./contracts \
  --build-arg GATEWAY_BASE_IMAGE=tyrag/ragflow:<release-tag> \
  -t tyrag/enterprise-gateway:<release-tag> \
  -f enterprise/gateway/Dockerfile enterprise

docker build --pull=false \
  --build-arg VITE_API_MODE=gateway \
  --build-arg VITE_UI_MODE=harness \
  -t tyrag/enterprise-web:<release-tag> \
  -f enterprise/web/Dockerfile enterprise/web
```

使用 `deploy/production/package-offline.ps1 -IncludeDiagnostics` 生成离线包。包内的
`image-manifest.json` 必须同时记录 source commit、RAGFlow upstream tag/commit、三个镜像
内容 ID/digest、Compose SHA-256 和补丁清单 SHA-256。30 上先校验 `SHA256SUMS`，再执行
`docker load`；不得用未登记的同名旧 tag 覆盖运行版本。

验收 runner 位于包内的 `verification/`，不复制进运行时镜像。需要在容器内使用服务环境
变量执行时，从开发机临时复制到 Gateway 容器的 `/tmp`，测试结束后删除：

```bash
docker cp verification/run_file_share_v3_v2_e2e.py \
  tyrag-production-enterprise-gateway-1:/tmp/run_file_share_v3_v2_e2e.py
docker cp verification/run_rag_diagnostics_e2e.py \
  tyrag-production-enterprise-gateway-1:/tmp/run_rag_diagnostics_e2e.py
```

runner 只输出脱敏结果，不得把 JWT、HMAC、API key、答案、Prompt 或文档正文带出容器。
完成后删除这两个 `/tmp` 文件；不要把 E2E runner 永久加入生产镜像。

导入后，仅使用原有 `.env`、`core.env`、`dev-ragflow.env` 和 `gateway-overrides.env`，并执行：

```bash
cd /home/zkadmin/tyrag-production
docker compose --env-file .env --env-file core.env \
  --env-file dev-ragflow.env --env-file gateway-overrides.env \
  -f docker-compose.yml \
  up -d --no-deps --force-recreate --pull never ragflow-cpu
```

确认 `ragflow-cpu` 的镜像 ID/digest 与发布清单一致，并在容器内检查本次 RAGFlow marker。
随后再按 §4 完成 PG 迁移和 Gateway recreate；RAGFlow 未通过 marker、health、9380/ping
或外部端口门禁时，不得继续更新 Gateway。

回滚：保留旧 RAGFlow tag，使用旧 tag 按同一 env-file 组合只 recreate `ragflow-cpu`，不删除
MySQL、ES、MinIO、Redis 数据卷。

---

## 6. enterprise-web

开发机常无 Docker。把 `enterprise/web` 打到 30 再 build。

```powershell
tar -cf - --exclude=node_modules --exclude=dist --exclude=.git -C enterprise/web . |
  ssh -i $env:USERPROFILE\.ssh\tyrag_zkadmin -o BatchMode=yes zkadmin@192.168.30.30 `
    "rm -rf /tmp/enterprise-web-src && mkdir -p /tmp/enterprise-web-src && tar -xf - -C /tmp/enterprise-web-src"
```

30 上：

```bash
set -euo pipefail
docker tag node:22-alpine node:22.17-alpine   # 若本地只有 22-alpine
docker tag nginx:1.27-alpine nginx:1.28-alpine
sed -i 's/\r$//' /tmp/enterprise-web-src/Dockerfile /tmp/enterprise-web-src/nginx.conf
cd /tmp/enterprise-web-src
docker build --pull=false \
  --build-arg VITE_API_MODE=gateway \
  --build-arg VITE_UI_MODE=harness \
  -t tyrag/enterprise-web:v0.26.4 \
  .

cd /home/zkadmin/tyrag-production
docker compose --env-file .env --env-file gateway-overrides.env \
  --env-file /tmp/offline-release-<commit>/release.env \
  -f docker-compose.yml -f docker-compose.30-release.yml \
  --profile diagnostics \
  up -d --no-deps --force-recreate --pull never enterprise-web
```

`--profile diagnostics` 不能少。必须带 `gateway-overrides.env`，否则 3000 会绑回 `127.0.0.1`。

验证：开发机打开 `http://192.168.30.30:3000/`，bundle 里能搜到本次 CSS/文案 marker。然后 `rm -rf /tmp/enterprise-web-src`。

---

## 7. RAGFlow 8080 前端（优先不重建容器）

重建 `ragflow-cpu` 曾经把 8080 打回 `127.0.0.1`，并弄丢 `API_PROXY_SCHEME`。前端-only 改动：

1. 本地 `ragflow/web` 构建出 `dist`
2. 打包 `dist`（不要 `.map`）scp 到 `/tmp/ragflow-web-dist.tar.gz`
3. `docker cp` 进 `tyrag-production-ragflow-cpu-1:/ragflow/web/`
4. 容器内解压覆盖 `dist`，必要时 `nginx -s reload`
5. **不要** `docker compose ... ragflow-cpu`

从开发机强制刷新 `http://192.168.30.30:8080`（Ctrl+F5），确认新 `index-*.js`。

只有镜像层必须变（entrypoint、系统包）才 recreate `ragflow-cpu`。那时：

```bash
cd /home/zkadmin/tyrag-production
docker compose --env-file .env --env-file core.env --env-file dev-ragflow.env \
  --env-file gateway-overrides.env \
  --env-file /tmp/offline-release-<commit>/release.env \
  -f docker-compose.yml -f docker-compose.30-release.yml \
  up -d --no-deps --force-recreate --pull never ragflow-cpu
```

之后立刻查 `0.0.0.0:8080`、`0.0.0.0:9380`、ping=`pong`、Gateway 仍 healthy。缺 `API_PROXY_SCHEME=python` 时不要反复 recreate 碰运气。

---

## 8. 部署后清单（缺一不可）

- [ ] 只重建了本次相关服务，`--no-deps --pull never`
- [ ] `ss`：相关端口仍是 `0.0.0.0`
- [ ] **开发机** 能打开 `192.168.30.30` 对应端口（不是只在 SSH 会话里 curl 127.0.0.1）
- [ ] 容器 health=healthy
- [ ] 线上代码/bundle 有本次 marker
- [ ] 含 schema 发版时：`gateway_schema_version` 已升到目标版本，且新表（如 `ext_identifier_history`）存在；**未** recreate mysql / 未删数据卷 / 未整链 up
- [ ] 业务验收已交 **Sim**（身份/现行事实类勿用哈希冒烟凑数）
- [ ] 未把 secret 打进终端、聊天、脚本仓库
- [ ] 上传到 `/tmp` 的源码包已删

健康但局域网不通：先改 bind（`.env` + `gateway-overrides.env` 写成 `0.0.0.0`），再按上面带 env-file 的 compose recreate **同一个** 服务，再从开发机复测。

---

## 9. 完成报告模板

- 修改的 30 侧对象（镜像 tag / 容器 / 是否 in-place dist）
- 备份 tag
- 开发机探测结果（8080 / 9380 / 5188 / 3000 的 HostIp + HTTP）
- marker 证据
- 是否改了上游
- 未做的事（例如没动 EAM、没提交 git）
