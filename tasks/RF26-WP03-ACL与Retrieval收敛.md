# RF26-WP03 ACL 与 Retrieval 收敛

## 目标

把 EAM/JWT 身份、ACL、已绑定设备、active/current version 与 quality passed 编译为 RAGFlow retrieval/completion 前的硬条件；RAGFlow 负责命中排序与生成，Gateway 不再靠召回后删结果“补权限”。

## 范围与非范围

范围：JWT→tenant/user、ACL scope、metadata/document 条件、官方 retrieval 调用和 completion 前复核。非范围：更改 EAM Query v2.9.0、全库召回、Text-to-SQL、设备数量/维修次数/实时状态精确查询、修改 RAGFlow ACL 模型或主 OpenAPI。

## 真实调用链

`EAM v2 message → Gateway JWT` → conversation ownership → Gateway 读取当前 ACL、绑定设备与可用文档状态 → 计算 `ACL ∩ equipment metadata(若已绑定) ∩ active/current ∩ quality-passed` → `POST /api/v1/retrieval`（dataset IDs 与 metadata 过滤）→ Gateway 再对每个候选 snapshot/版本复核 → `POST /api/v1/chat/completions`。若范围为空，Gateway 走既有无可靠证据路径，不请求全库候选。

## 接口与责任归属

Gateway Identity/ACL：认证、授权与条件编译。Gateway Retrieval：官方 retrieval 请求、候选复核、citation 输入。RAGFlow：仅在已传 dataset/filter 内检索和完成。EAM：不传可覆盖 ACL/设备范围的隐藏字段。Citation Snapshot 与原件访问仍由 Gateway 再授权。

## 精确实施任务

1. 以当前 ACL policy 产生允许 dataset/document/metadata 范围，空范围 fail closed。
2. 将 conversation 的 immutable equipment context 转成白名单 metadata 条件；未绑定时遵循现有 v2 规则，不能把用户问题拼成伪 ACL。
3. 统一 direct retrieval 与 chat completion 的同一可见集合，避免一个路径放宽。
4. 在调用 completion 前再次确认候选版本、质量和用户 ACL；拒绝已失效/换版本文档。
5. 将 RAGFlow reference 映射到 snapshot 输入时只保留外部可见字段；不泄露内部 IDs/存储位置。
6. 测试跨 tenant、无 ACL、组撤销、设备不匹配、质量失败和正常精确命中。

## 依赖、验收与回滚

依赖：WP00 API 基线、WP02 quality truth。验收：最小安全负例证明未授权文档永不出现在 retrieval、completion 或 citation；授权文档可命中；状态不由 citations 推断。回滚：回到上一已测 ACL compiler/查询实现，保持 fail closed；不以“先召回后过滤”临时恢复。

## Agent 目录所有权

Identity/ACL Agent：`enterprise/gateway/auth`、`acl`。Retrieval Agent：`enterprise/gateway/query`。两者协调接口由 Lead 收口；不得改 RAGFlow 官方数据库或全局枚举。
