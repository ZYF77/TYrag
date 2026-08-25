# CURRENT

当前收敛基线是 RAGFlow `v0.26.4`（`cb93883f3f8c975eecb2fed81210effeb3bdb06f`）。RF26-WP00～WP06 已实现：FILE_SHARE 使用官方 multipart + `/datasets/{id}/chunks`，`RF-PATCH-002` 全链退役且不迁移/删除历史；Gateway 保留 EAM 认证、ACL、业务状态/历史、质量门、回调和 Citation；Query/FILE_SHARE/Callback 的 EAM 外部 wire 仍为 v2.9.0/v3.1.0/v1.0.0。RF-PATCH-003/004 保留，新登记 RF-PATCH-005（WebSearch 故障回退/日志脱敏）和 RF-PATCH-006（临时 upload 认证删除）。

离线证据：P0 `768` Python + `134` Web 全通过，v2 smoke/tsc/py_compile/YAML/diff-check 通过；RAGFlow 容器内 WebSearch 回退 `2 passed`，附件 DELETE 等价断言通过。证据在 `artifacts/enterprise-tests/20260823T191805Z-40296/`。

当前唯一阻塞仍是 WP07 本机真实 HTTP E2E，但 Docker/额度阻塞已解除且服务已加载。`artifacts/e2e/local-http/20260824T013519Z/` 已证明官方 upload/chunks、解析质量门、callback 503→204 同 deliveryId、JSON/SSE/history、citation source、路径/SHA/ACL 负例；原 runner 的宿主 SQLite session 读取不适用于运行中的 Windows bind mount，现已改为 RAGFlow 官方 chat/session API，定点检查通过。后续全量复跑被 RAGFlow 容器无法解析默认及已配置 embedding 域名阻塞，尚无单次全绿 artifact，不能声明 WP07 完成。外部 embedding DNS 恢复后直接重跑 runner；不得连接或部署 `192.168.30.30`。
