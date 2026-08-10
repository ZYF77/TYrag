# M3-F 可靠性验收资产

## 1. 结论边界

本资产是平台可靠性验收的计划、契约校验和证据工具，不是外部环境替身。M2 当前仍为 blocked：没有真实 RAGFlow、Asset Registry、Redis/Valkey、对象存储和脱敏业务样本时，不生成 Integration 通过报告。

现有统一入口仍是：

```powershell
pwsh -File enterprise/scripts/run_enterprise_tests.ps1 -Profile Contract
pwsh -File enterprise/scripts/run_enterprise_tests.ps1 -Profile P0
pwsh -File enterprise/scripts/run_enterprise_tests.ps1 -Profile Integration
```

退出码保持不变：`0` accepted，`1` 测试/验收失败，`2` 正式样本或本地前置条件 blocked，`3` 真实外部环境缺失/不可用，`4` runner、报告或 `ragflow/` guard 失败。任何缺少在线依赖的 Integration 运行必须保留 `exit 3`，不得用 memory、mock、skip、xfail 或本地 fixture 改成绿色。

新增入口 `enterprise/scripts/run_upgrade_checks.ps1` 只编排既有 runner：

```powershell
# 升级前，必须先得到 Contract/P0 基线
pwsh -File enterprise/scripts/run_upgrade_checks.ps1 -Phase Before

# 升级后或回滚后，必须引用成功的 Before summary
pwsh -File enterprise/scripts/run_upgrade_checks.ps1 `
  -Phase After `
  -BaselineSummary artifacts/upgrade-checks/<before-run>/summary.json
pwsh -File enterprise/scripts/run_upgrade_checks.ps1 `
  -Phase Rollback `
  -BaselineSummary artifacts/upgrade-checks/<before-run>/summary.json
```

该入口不会执行升级、迁移或删除 volume；只运行 `Contract`、`P0` 并保留子 runner 的退出码，升级/回滚编排由部署流水线负责。

## 2. 性能基线

目标配置位于 `enterprise/scripts/m3f/performance_baseline.json`，状态固定为 `initial_target_not_measured`。它不包含客户正文、Prompt、模型响应、Token 或真实测量值。当前目标覆盖：

| 工作负载 | 并发矩阵 | 关键目标 |
|---|---:|---|
| ACL 检索 | 1/5/10/25/50 | latency `p50/p95/p99`、queue latency、吞吐、错误率 |
| 只读业务查询 | 1/5/10/25 | `p50/p95/p99`、有界结果/超时、吞吐、错误率 |
| 文档事件登记 | 1/5/10/25 | 登记与解析解耦、队列积压、`p50/p95/p99`、错误率 |
| SSE 控制面 | 1/5/10/25 | `run.started < 1s`、首内容/heartbeat、最大 heartbeat 间隔、错误率 |

正式测量输入只能来自真实部署的脱敏计时 envelope；只记录耗时、队列耗时、状态、并发、版本和依赖模式：

```powershell
python enterprise/scripts/m3f/reliability_metrics.py `
  --input <real-sanitized-measurement.json> `
  --output artifacts/performance/m3f-report.json
```

工具使用固定 nearest-rank 计算 `p50/p95/p99`，同时输出成功吞吐、尝试吞吐、队列延迟和错误率。没有 `--input` 时返回 `2` 并明确 blocked；不会生成合成结果。正式报告必须补充 Gateway 副本数、RAGFlow/Redis/Valkey/对象存储版本或模式、压测时长、数据规模和 ACL 范围。

容量测试至少执行稳定负载、阶梯并发、突发负载、队列积压、依赖超时、Redis/Valkey 故障和对象存储读写故障场景。外部回答模型耗时单独记录，不能混入检索 SLO。

## 3. 限流策略

`enterprise/scripts/m3f/rate_limit_policy.json` 是 `proposed_not_enforced` 的策略契约，不改变冻结的 v1/v2 OpenAPI 或 `contracts/error-codes.yaml`。实际实现前必须由 Lead 冻结 HTTP wire 错误码和 Gateway 执行位置。

| 维度 | 初始窗口/上限 | 作用域 |
|---|---:|---|
| tenant | 600 req/min，100 concurrent | 租户总量 |
| user | 120 req/min，8 concurrent | 用户请求 |
| api_key | 300 req/min，32 concurrent | 服务凭据 |
| cost | 100,000 cost units/hour | 租户成本预算 |

执行顺序为 `api_key -> user -> tenant -> cost`。保护路由的 reservation 必须在 Redis/Valkey 以原子操作完成，部分 reservation 失败必须回滚；不能先执行检索/模型调用再丢弃结果。Redis/Valkey 不可用时保护路由 fail closed，不能退回进程内内存；健康检查可不占用配额。超限返回候选 `429 RATE_LIMITED`、`Retry-After`、`X-RateLimit-*`，存储不可用返回候选 `503 RATE_LIMIT_STORE_UNAVAILABLE`。

离线负向测试覆盖租户、用户、API 凭据、成本四个维度的边界和超限，以及 Redis/Valkey 不可用 fail closed。它们只校验策略，不声明生产请求已被限流。

## 4. 状态化与故障转移评估

当前候选不是完全无状态：`enterprise/gateway/app.py` 持有模块级 SQLite connection，Gateway conversation/run 状态仍是 Enterprise 自有 SQLite，后台 worker 也在进程内启动。因此当前可接受边界是单 Gateway/多 worker；生产多副本不能以“加副本”宣称 HA。

生产无状态化前置条件：

- 把 Gateway conversation、run、idempotency、租约和审计写入独立 Enterprise PostgreSQL repository；不得使用客户业务 PG 或 RAGFlow 官方数据库；补齐 schema、连接池、迁移、备份和恢复证据。
- 将 worker/reconciler 从 Web 进程拆出，使用持久队列和租约；同一 run 只能由一个租约持有者执行，过期落为稳定 `RUN_INTERRUPTED`，不能无界重试。
- Redis/Valkey 只承载 replay protection、限流计数和短租约，不承载唯一业务事实；生产使用独立 logical DB/prefix、TLS/ACL、容量告警和 fail-closed 策略。跨实例 replay 仍需真实 `SET NX EX` 证据。
- 对象存储启用版本化、服务端加密、校验和、跨可用区/区域复制与生命周期；不产生长期公开 URL，原始业务文件的权威性仍归外部业务系统。
- live/ready 分离：live 只表示进程存活，ready 必须反映必须依赖的版本、数据库、Redis/Valkey、对象存储和文档引擎状态；故障转移期间只接收可安全重试的请求。

建议恢复顺序为：固定版本和配置引用 -> Enterprise 状态库 -> RAGFlow 官方数据库/文档引擎快照 -> 对象存储 -> Redis/Valkey（只恢复必要的非持久窗口） -> Gateway worker -> Contract/P0 -> ACL 负向与固定检索评测。Redis replay key 不应被当作业务备份；恢复后应在隔离 namespace 中重新建立窗口。

## 5. 备份、完整性与演练

`enterprise/scripts/m3f/backup_restore_drill.py` 只处理操作者显式传入的目录，拒绝 symlink、源/目标重叠和非空恢复目录，不执行递归删除。`manifest.json` 对每个相对路径记录大小和 SHA-256；命令输出只包含状态、计数和 RTO/RPO，不输出文件名、正文或凭据。

备份范围必须包括：RAGFlow 官方数据库、文档引擎快照、对象存储、当前 Enterprise SQLite（迁移到独立 PostgreSQL 后改为该状态库）、配置/Secret 引用、审计数据、固定评测集和版本清单。Docker volume 不是备份；备份必须离开原宿主机并可独立校验。

非破坏性演练入口：

```powershell
python enterprise/scripts/m3f/backup_restore_drill.py drill `
  --source-dir <mounted-export> `
  --backup-dir <external-backup-destination> `
  --restore-dir <isolated-restore-destination> `
  --source-watermark <RFC3339-source-watermark> `
  --latest-event-at <RFC3339-latest-accepted-event> `
  --rto-target-seconds 3600 `
  --rpo-target-seconds 900
```

MVP/Beta 初始目标为 RTO `<= 60 min`、RPO `<= 15 min`，正式目标需由容量和业务窗口确认。脚本只有在备份和恢复 hash、RTO、RPO 均有实测证据时才返回 `0`；缺失真实 watermark、目标或外部导出时不得补值。校验失败返回 `1`，证据/前置条件 blocked 返回 `2`。恢复后仍必须运行真实固定版本 Contract/P0、ACL 负向和检索评测，不能只凭 hash 宣称服务可用。

## 6. 升级与回滚检查表

升级前：

1. 核对 `version-manifest.json` 的 upstream tag、commit、image digest、文档引擎和迁移基线。
2. 完成外部备份和 manifest 校验，记录源 watermark、备份时间、RPO 计算依据。
3. 执行 `run_upgrade_checks.ps1 -Phase Before`，保存 Contract/P0 summary。
4. 在隔离副本执行官方迁移/启动，再逐项评估外围补丁；不得直接在生产首次迁移。

升级后：

1. 使用同一 `-BaselineSummary` 执行 `-Phase After`。
2. 核对 Contract/P0 均为 0、`ragflow/` guard 未变化、健康/ready 和版本清单一致。
3. 在真实环境补跑 Integration；依赖缺失保持 `exit 3`，不能用 P0 离线绿灯替代。
4. 比较性能 `p50/p95/p99`、吞吐、队列延迟、错误率、ACL 负向和恢复指标；超出预算停止切换。

回滚：

1. 在回滚窗口内切回已验证的旧镜像/外围 overlay；不删除旧 volume、索引或对象。
2. 如迁移不可逆或状态不兼容，按批准的恢复演练恢复隔离副本，禁止临时修改官方迁移。
3. 执行 `-Phase Rollback`、Contract/P0 和真实 Integration（若环境可用），记录实际 RTO/RPO 与失败项。

## 7. 资产与契约变化

- 新增 `enterprise/scripts/m3f/` 仅为 M3-F 验收工具和非敏感目标配置。
- 新增升级前后包装入口，复用现有 acceptance runner；现有 profile 名称和 `exit 3` 语义未改。
- 限流策略标记为 proposed，未修改主 OpenAPI、错误码、数据库迁移或上游实现。
- 本线程不修改 `ragflow/**`，不提交 `.env`、密钥、Token、Cookie 或客户数据。
