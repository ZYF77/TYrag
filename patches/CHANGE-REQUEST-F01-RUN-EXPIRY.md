# F01：运行中重复请求不得写入失败消息

状态：代码与测试已实现；Python 3.13 / PostgreSQL / HTTP 契约完整验收待执行；未部署。

## 成功标准与契约

Gateway v2 Chat 与 v1 Workflow 共用的运行租约检查，仅在本次条件 UPDATE 确认把过期 running 改为 failed 时插入失败占位。未过期重复请求维持 202 和相同 runId；过期维持稳定 503/RUN_INTERRUPTED。不改变 API、schema、租约时长或配置。

## 独立变更单元

- `enterprise/gateway/query/v2_store.py::mark_expired_run_interrupted`：用 UPDATE ... RETURNING assistant_message_id 取代先 UPDATE 再无条件 SELECT。空 RETURNING 只读取当前 run；成功转换才写入占位，两者沿用调用方同一个事务。
- `enterprise/tests/test_v2_conversation_contract.py`：仅两个 duplicate 测试的变更属于 F01。Chat/Workflow 分别覆盖未过期 202、零失败消息、原 assistant ID 可正常插入、过期重复回放只有一条失败消息。
- `enterprise/tests/test_v2_run_expiry.py`：独立 PG 连接竞争、转换次数、失败占位插入异常回滚、三个终态保持。
- `enterprise/tests/test_run_expiry_source.py`：实际 store 函数的无服务 AST 控制流探针，验证未转换/转换成功/无 assistant ID。不是 SQL 或 PG 并发验收。
- 完整审查报告和审查探针中的 F01 部分。

## 验证

无服务探针：`python3 -m pytest --noconftest enterprise/tests/test_run_expiry_source.py -q`，3 passed。

待固定 Python 3.13 与测试 PostgreSQL：

```sh
python -m pytest enterprise/tests/test_v2_run_expiry.py enterprise/tests/test_v2_conversation_contract.py -q
```

当前宿主 3.14.4，服务测试 collection 缺 pytest_asyncio，亦缺 SQLAlchemy/asyncpg/FastAPI，无 Docker 命令。未读取环境密钥、未安装替代依赖、未用 SQLite 替代 PG。PG 两连接测试已编写但尚未执行。

## 边界、集成与回滚

未修改 RAGFlow 上游，因此不新增 RF-PATCH 编号、不更改上游补丁 manifest。与 F12 可按独立文件/测试 hunk 审查提交。部署需在测试环境完成上述门禁后另行执行 Gateway 更新。

F05 的长任务续租、同会话不同请求串行化、迟到结果竞争仍独立待处理；本修正不宣称完成所有终态竞争控制。失败事务必须由现有 gw_write/GatewayDatabase.transaction 管理，不得拆成各自提交的两个事务。回滚仅回退该企业补丁，无迁移。
