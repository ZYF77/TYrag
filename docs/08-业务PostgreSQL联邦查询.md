# 08 业务 PostgreSQL 联邦查询

## 1. 原则

- 业务 PG 是设备事实和交易记录权威；
- RAGFlow 不直接 JOIN 业务 PG；
- LLM 不持有通用写权限或自由 SQL 权限；
- MVP 使用白名单 Query Adapter；
- 文档证据和业务记录必须分来源展示。

## 2. P0 数据域

1. 设备台账；
2. 固定资产台账；
3. 报修记录；
4. 维修记录；
5. 保养记录。

采集时序数据、复杂统计和财务信息不默认进入 P0。

## 3. Query Adapter

每个适配器定义：

- 业务用途；
- 输入 Schema；
- SQL 模板或存储过程；
- 允许字段；
- 最大时间范围；
- 最大返回行数；
- 超时；
- 数据脱敏；
- 权限条件；
- 输出 Schema。

示例：

```text
get_equipment_summary(equipment_id)
list_recent_repairs(equipment_id, limit<=20)
list_recent_maintenance(equipment_id, limit<=20)
get_fault_history(equipment_id, fault_code, from, to)
```

## 4. 综合问题流程

问题：

> AX-200 最近三次保养是什么？说明书要求多久保养一次？

执行：

1. 验证用户有权访问 AX-200；
2. `list_recent_maintenance(AX-200, 3)`；
3. RAGFlow 以 `equipment_id=AX-200` 和文档状态过滤检索保养周期；
4. 业务结果和文档证据分别结构化；
5. 回答模型明确区分“实际记录”和“手册要求”；
6. 返回业务 record citation 与 PDF citation。

## 5. 数据库安全

- 使用只读账号；
- SQL 固定参数化；
- 强制 tenant/department/equipment 权限条件；
- 设置 statement timeout；
- 限制结果行和字段；
- 禁止返回数据库内部密钥、连接信息和不必要个人信息；
- 查询审计记录 adapter、参数摘要、行数和耗时，不记录敏感正文。

## 6. Text-to-SQL 评估门槛

只有同时满足下列条件才进入 P2 评估：

- 业务方提供稳定指标定义；
- 建立只读语义层和表/字段白名单；
- 有 SQL 静态检查和成本限制；
- 有结果复核和权限注入；
- 有至少 50 个标注问题；
- 错误统计结果不会直接触发业务写操作。
