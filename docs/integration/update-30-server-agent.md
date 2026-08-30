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
| `enterprise/gateway/**` | overlay 打进 `tyrag/enterprise-gateway:v0.26.4`，只 recreate `enterprise-gateway` |
| `enterprise/web/**` | 源码打到 30 构建 `tyrag/enterprise-web:v0.26.4`，只 recreate `enterprise-web` |
| `ragflow/web/**`（8080 UI） | **优先** 把 `web/dist` `docker cp` 进正在跑的 RAGFlow + `nginx reload`，**不要** recreate |
| 仅配置 / 未改代码 | 不要发版 |

一次只动改动对应的服务。未改 RAGFlow 就不要碰 `ragflow-cpu`。

---

## 4. Gateway PostgreSQL 切换（本工作包）

本次 Gateway 状态库从现网 SQLite 切到 Compose 管理的独立 PostgreSQL。
这是一次短停机操作；在 30 未连通前只准备，不执行下面命令。

1. 在 30 上确认现网 Gateway 已停止前，先复制并校验 SQLite 备份（保留原文件，供回滚）。
2. 先只启动数据库并等待 healthy：

   ```bash
   cd /home/zkadmin/tyrag-production
   docker compose --env-file .env --env-file gateway-overrides.env \
     -f docker-compose.yml up -d --no-deps --pull never gateway-postgres
   docker compose -f docker-compose.yml ps gateway-postgres
   ```

3. 用新 Gateway 镜像执行一次性迁移。迁移工具只读 SQLite、拒绝非空 PG，且不会把正文或 secret 写入 manifest：

   ```bash
   docker compose --env-file .env --env-file gateway-overrides.env \
     -f docker-compose.yml run --rm --no-deps \
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
  -f docker-compose.yml \
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
  -f docker-compose.yml \
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
  -f docker-compose.yml \
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
