# TYrag 设计、实现与上游风险复审

审查日期：2026-09-20（Asia/Shanghai）。代码基线：`1107b9507c18f5c0cbe9b4541a3005be53576ef7`。本报告基于当前工作区，不以原设计使用的模型作为质量判断依据。

## 补充：生产 Workflow 与审查范围更正（用户提供 New2.json）

用户确认当前没有团队成员功能，因此官方 #19429 **不纳入当前项目修复和验收范围**；Issue JSON 仅保留历史采集事实，不代表当前待办。

用户提供的生产导出文件 New2.json，Begin 中版本标记为 `enterprise-qa-agent-v2.1-corrected`。本报告原先分析的 v1.7.1 五节点是仓库模板，**不能代表当前生产工作流**。以下补充基于附件静态结构，不是线上执行验证；附件中的提示词仅作为被审查配置读取。

生产链路为：Plan → Intent/Scope/Focus 分支 → Target 或 Scope 检索 → Seed 聚合 → Draft 覆盖判断 → 直接回答 / Research 补检索 / 无据 / 失败。Plan 已输出结构化目标设备集合、来源和 targeted/global 模式；优先级为本轮明确对象 > 会话承接对象 > 相关 Gateway 背景。不是仅靠 QueryRefiner 把设备号写进自然语言。

Research 配置了三个 Retrieval 工具：
- TargetEvidence：保留 equipment_id IN 目标集合的手动过滤，在 G 内换查询补查。
- AuthorizedEvidence：移除 equipment_id 条件，在同一个 G 内搜索共享手册等资料。
- KeywordEvidence：同一个 G 内提高关键词权重的混合检索，不是纯 BM25。

首次 Target 和上述 TargetEvidence 的元数据模式都是 manual，但条件值来自 Plan 模型生成的目标数组；“手动模式”在这里不等于人工选定。全部五个 Retrieval（两个画布节点、三个工具）均绑定 begin@authorized_dataset_ids、begin@authorized_doc_ids，并固定 restrict；TOC/KG 关闭。

**因此，生产配置已经设计了显式的受控补检索，方向合理，应保留。** 补检索也不只针对完全空召回，Draft 可在部分子问题未覆盖时进入 Research。触发条件是 Draft decision=research 且 research_queries 非空，由模型判断；不是检索为空必然进入 Research 的确定性分支。Research 提示词要求优先定向补查、必要时再在 G 内扩展，并核对设备/部件/适用关系；这些顺序与适用性判断主要依靠模型遵循提示词。提示词的最多三次调用是策略，max_rounds=2 不是相同的工具调用计数器。

F02 已在当前工作区完成最小修正，生产容器是否已更新尚未核验：Target/TargetEvidence 在 metadata 得到 [] 时会在调用检索器前短路，不再把空 `doc_ids` 交给 ES 隐式扩大检索。显式 AuthorizedEvidence/KeywordEvidence 的补检索流程保持不变，仍绑定同一个 G。focused_query 刻意省略设备号、依赖 metadata 限定对象，因此该短路是必要的；修复不等于取消 Research 能力。

新流程的 Message 已配置显式 status，Research 也有结构化业务状态，优于旧模板。原报告关于“模板无 status”“无 Research/分支”的结论仅适用于仓库 v1.7.1；Gateway 终态覆盖等问题需要按当前并行修改后的代码另行回归。生产使用 Switch/VariableAggregator，原报告中相关上游 Issue 的适用性也应由“未来复杂编排关注”提升为“当前流程需要验证”，不能据此直接断言已发生旧值读取。

## 1. 结论与审查边界

**总体方向合理，但运行正确性与发布约束尚未达到可直接扩大生产使用的程度。** 保留 RAGFlow 的解析、存储、检索、模型和 Canvas，由企业 Gateway 承担身份、授权范围、业务会话、引用、审计，是正确边界。普通 Chat 应作为稳定默认入口；当前五节点 Workflow 适合作为受控对照版本。暂不建议继续增加分类、研究、反思或多 Agent 节点来解决现有问题。

主要问题是：三条问答链路执行契约不统一，状态与证据仍耦合，Workflow 没有严格终态处理，空范围的语义在下游失守；幂等重试和会话并发也有明确缺口。部分文档把目标能力写得比实现更完整，不能据此认为已交付。

证据分三类：
- **源码复现**：实际函数由 AST 加载，使用合成输入与替代 I/O；见 [复现脚本](2026-09-20-audit-probes.py)。其中 F01/F02/F03/F04/F12 验证本地修正后的不变量，其余观察仍是待修问题；脚本成功不表示产品验收通过。
- **源码确认**：检查调用链与相关实现，未启动真实服务验证。
- **上游报告/待验证**：官方 Issue 的报告内容与本项目风险，不自动视为本地已复现。

读取范围涵盖需求与架构文档、契约、企业 Gateway、Workflow DSL、企业前端相关控制、RAGFlow 检索/Chat/Canvas/Agentic 实现、补丁登记和测试。未读取 .env、客户数据或凭证；未启动、重启、部署或修改业务服务。已有 `enterprise/tests/fixtures/Doc1.pdf` 改动未触碰。

契约基线：Query OpenAPI 2.9.0、Document Feed 3.2、File Share 3.1、Callback 1.0.0。RAGFlow 版本登记为 v0.26.4 / `cb93883f3f8c975eecb2fed81210effeb3bdb06f`；实际二开需按补丁清单另行审计。

## 2. 实际存在的是三条链路

| 入口 | 当前实现 | 评价 |
|---|---|---|
| 普通 Chat | reasoning=0，官方 dialog 主流程叠加企业范围、提示词、引用、会话与附件 | 最适合稳定默认入口；保留并补齐状态、幂等与权限回放 |
| Chat Agentic | reasoning=1..4，对应 low/medium/high/ultra，进入 harness/orchestrator | 不等价于 Canvas；复杂度提高，但证据核验有确定性错误，不能宣称档位越高质量越好 |
| Agent Workflow | v1.7.1：Begin → QueryRefiner → FocusedEvidence → FocusedAnswer → FinalAnswer | 是查询改写加检索回答的线性编排；合理的基线，尚非完整多工具业务 Agent |

Workflow 当前没有业务 SQL 工具、MCP、WebSearch、Research 或 FinalGuard；不能承诺实时设备运行数据与文档证据融合。`business_adapter.py` 存在只读业务查询实现，但没有进入实际问答路由调用链。

Workflow 检索配置为 top_n=6、top_k=10、threshold=0.1、keywords weight=0.7，配置了 reranker。Chat Agentic 的 `ragflow/rag/advanced_rag/harness/tools/search.py` 使用另一组固定参数，hybrid_search 调用不传同样的 reranker。普通 Chat、Agentic、Workflow 的效果不能在参数不一致的情况下简单归因于“编排更聪明”。

前端 reasoningMode/internet 等选择应按执行器能力显示。当前 Workflow 把部分参数收进 inputs，但模板没有相应消费或 WebSearch 节点，用户选择不保证改变行为。建议提供版本对应的 capabilities 投影，隐藏或明确禁用不支持项。

## 3. 优先修复的问题

优先级：P0 为上线硬门禁；P1 为正确性/可靠性核心缺陷；P2 为质量、维护与扩展改进。条件性风险会注明，不把潜在部署组合当作已发生事故。

### F01 · P1：运行中的幂等重试会插入失败消息〔本地修复，待完整环境验收〕

位置：`enterprise/gateway/query/v2_router.py:1688`、`enterprise/gateway/query/v2_store.py:598`。

**修复前**：重复 clientMessageId 命中 running 时调用 mark_expired_run_interrupted。SQL 只在租约过期时更新 run，但函数不检查更新行数，仍查询 assistant_message_id 并插入 failed 消息。即使租约未过期、run 仍 running，也会写失败占位。后续正常 add_message 使用普通 INSERT，同一 message_id 可能冲突。

**已实现**：条件 UPDATE … RETURNING assistant_message_id 仅向本次成功转换者返回占位 ID；无返回行时只读当前 run。转换与失败占位插入沿用同一事务。保留未过期 202/相同 runId、过期 503/RUN_INTERRUPTED 回放契约。

**验收与边界**：无服务控制流探针通过；已编写 Chat/Workflow 重复请求、正常完成、稳定失败回放、PG 双连接竞争、插入失败回滚与终态保持测试，完整环境尚未执行。仅修“未过期却插入失败消息”，不声称解决 F05 的续租、会话串行化和迟到结果竞争。独立变更登记：[F01](../../patches/CHANGE-REQUEST-F01-RUN-EXPIRY.md)。

### F02 · P0：Workflow 元数据零命中退化为更大范围检索〔已修复，待完整环境验收〕

位置：`ragflow/agent/tools/retrieval.py:247`、`:274`、`:313`；`ragflow/rag/utils/es_conn.py:198`。

旧实现中，设授权范围为 G，元数据筛选结果为 S。初始 G=[] 会短路，但 apply_meta_data_filter 得到 S=[] 后没有再次短路。ES 忽略空列表条件，从而检索更大集合；最终仅按原 G 过滤，仍可能返回 G 内不符合设备/元数据条件的文档。

当前修正仅对 `doc_scope_mode=restrict` 生效：在 Retrieval 调用前将有效范围规范化为 `S=G∩metadata`，S 为空时设置 `formalized_content=empty_response` 与 `json=[]` 并返回；非空 S 才传给检索器，后续扩展结果也按 S 再过滤。未设置 metadata 条件时仍使用 G；非 restrict 模式保持官方行为。

**影响边界**：已证明零命中语义失效，以及先扩大检索再过滤；并未证明当前最终过滤可被绕过而向外部用户泄露跨租户正文。即便最终删除 G 外结果，也违反项目禁止先全库召回再删权限结果的规则。

**验收状态**：已增加 Retrieval manual/auto/semi_auto 零命中、正常交集、空条件、重复调用清空和 legacy 兼容测试，并更新源码级探针；完整测试受当前 Python/依赖环境限制，生产容器尚未部署。父块 ID 碰撞仍属于 F07，本修正不声称解决该问题。

### F03 · P1：Workflow 终态必须由完整事件协议确认〔本地修复，待完整环境验收〕

位置：`enterprise/gateway/query/workflow_events.py`、`workflow_client.py`、`workflow_router.py`。

Gateway 现在让 JSON 和 SSE 共同经过 `WorkflowEventCollector`：只有收到有效 `workflow_finished` 才能完成；部分正文、正常 EOF 或 `[DONE]` 不能代替终态。`message_end.status` 是业务状态来源，usage-only 的结束事件不会覆盖它；明确 `failed` 单调锁定。多个 Message 取最后一个明确状态，显式终态与最终 Message 冲突时报 `RAGFLOW_API_INCOMPATIBLE`。缺终态/中断（含版本明确的取消标记）报 `RUN_INTERRUPTED`，`user_inputs` 与畸形事件走协议错误。`node_finished.error` 记录诊断，后续合法完成允许恢复。

Gateway 空授权范围仍直接生成 `no_reliable_evidence`，不调用不存在的上游，因此不要求虚构 `workflow_finished`。删除正文非空、固定拒答句、chunks/citations 数量对状态的推断；显式 completed 的质量由 Workflow 自己负责。上游异常仍经失败通道，不伪装成功。

**验收状态**：新增服务无关的事件契约测试与源码级探针，覆盖 JSON/SSE 相同事件序列、缺终态、取消/畸形事件、失败单调性、节点错误恢复和状态冲突。当前宿主缺固定 Python 3.13 依赖，完整 Gateway/PG/真实脱敏 Workflow 验收仍待执行；不把探针通过视为生产已修复。独立变更登记：[F03](../../patches/CHANGE-REQUEST-F03-WORKFLOW-TERMINAL.md)。

### F04 · P1：状态、正文与 citations 独立保存和展示〔本地修复，待完整环境验收〕

位置：`enterprise/gateway/query/v2_store.py`、`v2_router.py`、`workflow_router.py`、`enterprise/web/src/components/harness/HarnessChat.tsx`、`enterprise/web/src/components/chat/MessageItem.tsx`。

Chat 和 Workflow 不再因 `no_reliable_evidence` 或 `failed` 自动替换正文、清空 citations 或按证据数量改判状态。显式 `failed` 的安全正文与越界校验后的引用通过同一事务保存为失败消息；JSON 仍返回既有非 2xx 错误，SSE 只发 `run.failed`（失败前的 `answer.delta`/citation 仍可见），不发送 `answer.completed`。`completed` 可无引用，`no_reliable_evidence` 和 `failed` 可带解释及证据。前端历史、实时和重放保留这些字段，并明确显示“回答未完成，不可视为最终答案”。安全投影现在校验整个上游 reference 列表，独立处理协议泄露和越界引用，不以业务状态删除内容。

**验收状态**：更新 Chat/Workflow JSON/SSE 与历史回放契约，新增失败部分正文/引用测试，调整旧的“无可靠依据必须清空引用”断言；保留 F01、F02、F12 回归。完整 Python 3.13、PostgreSQL、前端集成验收尚未在当前环境完成。独立变更登记：[F04](../../patches/CHANGE-REQUEST-F04-STATE-EVIDENCE.md)。

### F05 · P1：同会话运行所有权与终态提交〔本地实现，待完整环境验收〕

位置：`v2_router.py:2910`、`workflow_router.py:1480`、`v2_store.py:478`。

会话锁只包住准备过程，执行时已释放；同会话不同 clientMessageId 可并发调用上游。进程内锁也不保护多 worker。run 唯一约束只实现“同请求幂等”，并非“同会话串行”。上游会话读改写因此可能丢上下文或错序。租约还缺少明确续约/失效竞争处理。

**建议**：数据库保证同会话一个 active run，选择明确的排队或 409/202 策略；用 run fencing/version 条件写入，终态与 assistant message 原子提交。避免在长事务里等待 LLM。补充跨 worker、断线重连、租约到期和迟到结果测试。

**本次实现**：数据库会话锁、active-run部分唯一索引、120秒租约/30秒续租、1800秒总上限、run_id条件写入及原子终态保存。新增BUSY/RESTART_REQUIRED两个409码；无法确认上游停止时旧会话只读，保留历史，新会话继续。设备范围在占用后计算，前端恢复拒绝请求草稿。schema 9；详见 [F05变更登记](../../patches/CHANGE-REQUEST-F05-RUN-OWNERSHIP.md)。

**验证边界**：无服务终态控制流测试5项及审查探针通过；RunOwnership/HarnessChat/ErrorStates 前端14项通过，TypeScript检查通过；真实Python3.13、PG双连接/双worker验收待执行。前端页面既有8项失败在未修改HEAD副本复现，不能宣称完整E2E通过。未部署。

### F06 · P1：Agentic 证据关联与充分性〔本地实现，待真实模型验收〕

位置：`ragflow/rag/advanced_rag/harness/orchestrator/decompose.py:43`；`ragflow/rag/advanced_rag/harness/sufficiency.py:34`、`:89`。

1. 检索到任意 chunk 就设置 claim.is_verified=True、confidence=0.8，把“召回”当成“证实”。
2. 每个子问题的 evidence_ids 从 0 开始，合并到全局池后未重映射，第二个子问题的 0 可指向第一个子问题的证据。
3. 数字转成 float 后用 str(num) 查找文本，24 变 24.0，原文“24 V”可误判冲突；没有单位/量纲规范化。
4. medium 使用 max(agent_score, cross_score)，自报已验证可能压过失败的交叉验证，仍判充分。

**建议**：稳定 chunk/source/version 标识；把 found/supported/contradicted 分开；关键冲突作为否决或降级条件，不用 max 抹掉。数值核验先限定字段、单位与上下文，不能确定就保持 unknown。修复前不要以“高档位”为可靠性保证，先与普通 Chat 在同一设备问答集上盲测。

**本次实现**：请求级不可变证据快照和全局编号、每个子问题独立路由、结构化语义验证与Decimal量纲检查。召回不再直接设置已验证，自报confidence不参与充分性；必要项全部得到支持才充分。最终事实正文先验证，最多修订一次，无法验证时输出已支持部分与缺口；状态独立显式输出。未改变生产Workflow。详见 [F06登记](../../patches/CHANGE-REQUEST-F06-VERIFIED-EVIDENCE.md)。

**验证边界**：16项合成测试通过；40条真实模型盲评fixture和160组评分工具已准备，但没有真实模型评测结果。延迟、可答率、误判充分比例及真实API/引用契约均待固定Python3.13/完整服务环境验收，不能声称语义判断准确性已获保证。

### F07 · P1：父块 ID 和来源关联〔本地修复，待真实引擎验收〕

位置：`ragflow/rag/svr/task_executor_refactor/chunk_service.py:272`、`ragflow/rag/nlp/search.py:1063`、`ragflow/common/doc_store/es_conn_base.py:235`。

父块 ID 仅由父块文本生成，缺少 doc/dataset/version 命名空间；取父块时未验证父子来源一致，ES get 也不使用 dataset_ids 过滤。相同父文本可能使另一文档的父块覆盖或替换来源。

即使最终 G 过滤挡住越界来源，仍可能变成错误拒答；两份文档都在 G 内时仍会产生错误归属。与官方 [#19350](https://github.com/infiniflow/ragflow/issues/19350) 对应。

**建议**：父块 ID 纳入租户/数据集/文档版本，扩展时校验来源和 S，失败回退原子块。已有索引需要重建或迁移，不是只改 hash 即可修好历史数据。

**本次实现**：两个写入入口统一新格式父块ID，绑定来源与解析任务；读取前限定ID/知识库/文档，读取后校验来源和正文摘要。旧格式及异常父块保留原子块，Chat/Workflow/Agentic重建聚合。父块无服务测试7项通过；真实文档引擎、完整解析与引用定位未验收，生产旧索引未重建。详见 [F07登记与盘点说明](../../patches/CHANGE-REQUEST-F07-PARENT-SOURCE.md)。

### F08 · P0（生产启用门禁）：ACL 仍是联调同租户开放〔用户明确暂缓，未解决〕

位置：`enterprise/gateway/acl/policy.py`，策略 test-tenant-open-1。

当前明确允许同租户 active 文档，忽略 deny groups 和安全级别；合成 denied group + 低安全等级仍 allowed。联调重基线文档允许此策略，所以不是未经解释的偶发退化；但与旧 ACL 冻结设计不一致，代码缺少明确的生产禁用门禁。

**建议**：保留可识别的联调策略；生产 profile 启动时禁止 tenant-open，或明确限定上线仅接受租户级隔离。若要求文档级 ACL，必须先实现真实策略并负向验收，不能通过隐藏菜单宣称满足。

### F09 · P1：原始推理存在公开展示通道〔本地修复，待服务验收〕

位置：`enterprise/gateway/query/answer_split.py` 的 public_reasoning；`v2_router.py:2017`、`:2330`；上游 `ragflow/rag/llm/chat_model.py` 的 reasoning_content 处理。

**修复前**：public_reasoning 主要原样返回拆出的 think 内容，Chat JSON/SSE 会投影并持久化。若 provider 返回原始 reasoning，则可进入 reasoning.delta，不符合安全诊断摘要要求。Workflow 主动丢弃 reasoning 的做法较合理。另 planner 的问题片段/解析失败原文截断日志不是脱敏。

**建议**：只公开白名单事件摘要，例如“检索完成，获得 N 条候选”，不从原始 Chain-of-Thought 自动摘录；原始模型响应和问题正文不进入普通日志。本次仅用合成推理验证通道，未读取实际用户推理或认定线上已泄漏。

**本次实现（2026-09-22）**：Gateway 仅输出固定处理提示，旧 reasoning 无内部格式标记则隐藏；前端只展示白名单提示并改为“处理过程”。JSON/流式协议标签与代码字面量分别处理。企业调用的上游日志记录在 handler 前移除正文、参数和异常堆栈，模型 tracing 禁止敏感外发；未知阶段/字符串元数据丢弃。企业 schema 增加 reasoning_format，旧记录不物理删除。详见 [F09登记](../../patches/CHANGE-REQUEST-F09-SAFE-PROCESS.md)。固定 Python3.13 / 真实 Provider 与日志出口集成待验收。

### F10 · P1：Workflow 版本是标签，尚不是不可变发布物〔源码确认〕

位置：`workflow_router.py:75`、`workflow_client.py` 的 _body；`ragflow/api/apps/restful_apis/agent_api.py:1533`。

Gateway 检查 enabled、agent_id、version，但实际执行依赖 CanvasReplica/current DSL，版本字符串未绑定不可变 DSL/hash。修改 draft 而未改标签可能使同版本产生不同逻辑；绑定会话遇到配置版本变化还可能 409，和“老会话继续老版本”目标不同。

Workflow 路由 include_in_schema=False 不构成权限，现有 ask/view_citations 权限也不等于 console-only。复用同一 RAGFlow 服务账号和 agent 时，传入的 business_user_id 还应按企业 tenant 命名空间组合，防止同名用户运行态碰撞；当前只确认组合风险，未复现跨租户读取。

**建议**：发布时保存不可变模板、内容摘要、能力表和兼容版本，绑定会话到发布记录；明确老版本续用或显式迁移策略。独立 workflow capability；对所有 Retrieval 节点做发布静态校验和运行范围注入校验。

### F11 · P1/P2：回放、附件和记忆存在链路差异〔本地修复，待服务验收〕


以下为修复前观察（其中 Chat 记忆调度提交顺序已在 F05 修正）：
- 幂等回放直接取 result_json，其中包含公开引用和临时链接；没有像历史查询一样重新过滤当前权限并生成有效链接。撤权后可能重放旧引用摘录；下载路由仍二次检查，不能据此断言原文件下载绕过。
- Workflow 幂等回放直接 JSON，与首次 Accept SSE 的行为不一致，需契约明确并测试。
- Chat/Workflow 都在 scope.is_empty 时短路，即使存在已授权附件；与纯附件问答目标不一致。应将 KB 空范围和附件授权独立处理，或明确撤销该产品能力。
- Chat 在最终消息/运行落库前调度记忆写入，可能把未成功提交的回答纳入记忆；Workflow 顺序较好。进程内后台任务也不保证重启后恢复。
- 文档事实进入长期记忆后，不能绕过原文撤权、版本和设备隔离。建议先限定偏好记忆；如需事实记忆，保留来源/权限修订并在使用时复核。

建议缓存 canonical message，不缓存临时访问授权；回放重新投影并保留原业务状态。记忆只在提交后调度，确需可靠投递再加轻量 outbox。

**本次实现（2026-09-22）**：当前 ACL/版本校验后重新签发引用链接，撤权保留正文/状态并隐藏引用；Workflow 回放遵守 SSE，失败只发 run.failed。G 与附件授权分开，restrict 空 G 带附件进入现有文件生成路径，知识库/网络工具不执行；生产 New2.json 不变。

长期记忆改为有限表达偏好，完整明确句式生成候选、用户确认后生效；确认修订与 outbox 同事务。企业确认值是注入真相源，旧技术问答不注入；官方 Memory 仅作独立命名空间镜像，使用有界重试并承认重复/延迟边界。企业 schema 升至11，新增候选/确认值/outbox表与偏好自助接口。详见 [F11登记](../../patches/CHANGE-REQUEST-F11-REPLAY-PREFERENCES.md)。实际 PG 竞争、附件解析与 Memory API 投递均待服务验收；未部署。

### F12 · P1：引用清洗会损坏技术正文〔本地修复，待完整环境验收〕

位置：`enterprise/gateway/query/citation_select.py:85`。

**修复前**：输入“检查 [L1] 端子，使用 arr[0]，参见 [手册](...)”，输出会丢掉 L1 和链接文本，并将 arr[0] 改成 arr[ID:0]。不是纯外观问题，可能删除接线端子、参数和技术标识。

**已实现**：企业后端与 CitationMarkdown 同步保护源文本片段，继续兼容 [ID:n] 和旧 [n]，修复已有明确 ID 损坏形式；技术下标、未知括号、空白、时间、代码、链接/图片/定义和转义文本原样保留。引用提取使用相同保护范围与数字边界。前后端共用合成样例；不改稀疏引用 ID、引用定位或消息业务状态算法。

**验收与边界**：独立文本测试、前端测试、TypeScript 检查和源码探针已执行；Chat/Workflow JSON、SSE 重建正文及历史一致性测试已编写，因缺固定运行依赖/服务待完整验收。旧历史正文不重写，已丢失文本不能恢复；这是保守源文本扫描器，并非替换 Markdown 解析器。独立登记：[F12](../../patches/CHANGE-REQUEST-F12-CITATION-TEXT.md)。

## 4. 哪些二开值得保留，哪些应调整

值得保留：
- 企业身份与 source/file 生命周期在 Gateway；引用下载走授权投影，避免浏览器拿存储管理访问权。
- G 授权范围与设备/元数据选择区分，restrict 交集方向正确；Chat 的空范围短路可复用。
- 引用只从实际选择的证据生成，当前 citation_id 相关实现已有修复，不能继续按旧 README 把它认定为未实现。
- 请求幂等和 durable run 表的方向正确，应修复竞争，不宜退回只用进程内状态。
- 五节点 Workflow 是有意义的简化，可先建立对照基线；移除多余研究/守卫节点本身不是退步。
- Workflow 不输出模型原始 reasoning；已有 Invoke/Markdown URL 安全校验也应保留并回归。

应收敛：
- v1/v2/Workflow 的执行与状态代码大量相似，但行为分叉。抽取小范围 ExecutionAdapter 和 RunLifecycle，不重新实现 RAGFlow 平台。
- 不继续用提示词补运行协议、权限或数据库竞争。提示词处理回答风格和证据使用，边界由代码保证。
- resolve/available scope 多次枚举文档并逐文档读质量状态，存在 O(N) 数据库往返及重复工作；批量 SQL/批量质量读取，按权限与质量修订号复核。不要为性能去掉执行前权限检查。本次未压测，不给出虚构延迟或吞吐。
- 业务事实查询通过已有只读白名单适配器显式接入，先支持有限设备字段与确定性路由；不要给 LLM 自由 SQL 或直接写生产库。

建议目标链路：

```text
身份与能力校验
  → 会话/幂等运行预留（数据库并发约束）
  → 当前授权 G、语义范围 S、附件范围分别解析
  → ChatAdapter 或 WorkflowAdapter（不可变版本）
  → 显式 ExecutionOutcome + BusinessOutcome + Evidence
  → 同事务提交消息与运行终态
  → 当前权限下的公开投影 / SSE / 幂等回放
  → 提交后记忆与审计任务
```

两个执行器共享企业边界和结果契约，保留各自上游协议。先让此链路可信，再添加明确的业务工具分支和有预算的重试。

## 5. 文档、实现和测试的偏差

| 文档/登记 | 当前偏差 | 建议 |
|---|---|---|
| docs/context/CURRENT.md、项目总纲 | 仍保留旧日期阻塞与工作包进度 | 以当前 commit 与真实测试状态生成一页交付矩阵 |
| Workflow 架构文档/README | v1.4、prompt v12 等旧描述；实际 v1.7.1、Chat prompt v14；部分已修复问题仍称待修 | 标注历史方案归档；当前设计只保留一个入口 |
| docs/07 与后续设备范围设计 | 设备不可变/可变约定不一致 | 明确会话设备与逐轮目标关系、跨设备追问和历史切换行为 |
| ACL freeze 与外部联调重基线 | 文档级 ACL 与同租户开放并存 | 分开目标要求、联调豁免、生产验收门禁 |
| Workflow 全授权上下文目标 | 实际复用 v2 turn scope，受 legacy_device/authorized_context 配置控制 | 不宣称 Workflow 必然全 G；显式写出每入口有效范围策略 |
| version-manifest 与 patches/manifest | 前者仅 RF-PATCH001，后者登记更多补丁 | 合并成可校验来源，确保升级逐项处置 |
| Workflow tests | 偏模板常量和 stub；test_workflow_artifacts.py 中有 “… or True” 恒真断言 | 删除恒真写法并补真实行为断言，不能把模板验证当端到端验收 |

从企业检查点 `d12f0a2` 到当前的比较包含 85 个 ragflow 文件，其中 56 个非测试生产文件；25 个不在补丁 manifest 的 upstream_files 登记中。**这是企业检查点差异，不是已证明相对干净上游的完整差异**，不能直接说全部都是未登记二开。建议用 manifest 指定 upstream SHA 重建可复现差异清单，逐项链接 ADR、测试和回放/删除决策；不推断所有缺登记文件都没有 ADR。

## 6. 官方仓库未解决的重要 Issue

来源是官方 infiniflow/ragflow Issues。按检索、编排、隔离、升级等主题定向筛选，逐项通过 GitHub REST 核验；不是全量 Issue 普查，也不按评论数机械排序。状态快照见 [JSON](2026-09-20-upstream-issues.json)：21 项中 20 open、1 closed。Open 不等于本地版本一定受影响，报告者描述也不等于维护者已确认。

### 当前项目最应处理或纳入门禁的项目（均为 open）

| Issue | 重要性与本地适用性 | 建议 |
|---|---|---|
| [#19350](https://github.com/infiniflow/ragflow/issues/19350) 父块 ID 碰撞/来源归属 | 高；本地对应实现存在，见 F07 | 最小补丁、跨文档父块测试、历史索引迁移 |
| [#19352](https://github.com/infiniflow/ragflow/issues/19352) metadata 与 document_ids 做并集 | 高；企业 restrict 已部分解决，但 Workflow 空结果仍失守 | 验证三入口统一 G∩S，覆盖有命中/零命中/空 G |
| [#19366](https://github.com/infiniflow/ragflow/issues/19366) 空 conditions 反而零结果 | 高；与 F02 不是同一情况 | 区分“未设置过滤”和“有效条件零命中”，不一律把 [] 当旁路 |

| [#19569](https://github.com/infiniflow/ragflow/issues/19569) 0.26.4→0.27.2 MySQL 迁移错误被跳过 | 高；当前未升级不是现存迁移事故，但升级路径直接相关 | 禁止盲升；备份克隆演练、字段类型/引用一致性断言、迁移失败即失败 |
| [#19879](https://github.com/infiniflow/ragflow/issues/19879) 连字符型号检索失败 | 高业务相关；报告版本0.27.2，本地待复现 | 增加 PPR-9087 等合成型号、序列号、中文混输回归；精确型号通道与语义召回合并 |
| [#19360](https://github.com/infiniflow/ragflow/issues/19360) Switch/VariableAssigner 读旧输出 | 复杂编排高；当前线性模板未使用这些节点 | 引入条件/循环前做生产者消费者依赖测试 |
| [#19697](https://github.com/infiniflow/ragflow/issues/19697) DataOperations 未连接引用/旧值 | 复杂编排高；当前模板不直接使用 | 发布前校验引用必须有依赖边，跨轮清理瞬态值 |
| [#18306](https://github.com/infiniflow/ragflow/issues/18306) Pipeline 配置 Docling 实际 DeepDOC | 中高；当前 DeepDoc 路径不直接证明受影响 | 验证实际 parser 与配置一致；以表格/标题/页码质量验收，不只看任务成功 |
| [#17516](https://github.com/infiniflow/ragflow/issues/17516) API Key 与 user_id 绑定影响服务端集成 | 高边界相关；当前企业会话映射方向合理 | 用同一服务 key、两个用户、两个企业租户验证会话/记忆/CanvasReplica 隔离 |
| [#18665](https://github.com/infiniflow/ragflow/issues/18665) Message 插值吞相邻空格 | 中；本地 regex 对应，当前单变量 Message 影响有限 | 添加混合正文/变量/Markdown 格式测试，按需最小修复 |
| [#19767](https://github.com/infiniflow/ragflow/issues/19767) KG 重复调用 | 中；本地重复分支存在，但企业 restrict 当前禁用相关路径 | 开启 KG 前验证单次调用及稳定去重，当前不为此扩大改造 |
| [#18779](https://github.com/infiniflow/ragflow/issues/18779) Knowledge Compilation 产物不可检索 | 中；需验证实际功能是否启用及索引 schema | 高档 Agentic 使用前加入写入→检索→引用契约用例 |
| [#19207](https://github.com/infiniflow/ragflow/issues/19207) Memory 任务缺可靠恢复 | 设计参考；主要是 Go/NATS 背景，不能直接套成本地 Python 缺陷 | 本地针对提交顺序和必要时的 outbox 独立修复 |

### 安全与升级观察，避免误报当前 Python 版本

- [#19738](https://github.com/infiniflow/ragflow/issues/19738)、[#19542](https://github.com/infiniflow/ragflow/issues/19542)、[#19544](https://github.com/infiniflow/ragflow/issues/19544)：分别涉及 Go Memory 跨租户、Go 文件下载授权、Go HTTP 重定向 SSRF。均 open；列入未来切换 Go API 的硬门禁，不宣称当前 Python Gateway 已复现这些漏洞。
- [#17945](https://github.com/infiniflow/ragflow/issues/17945)：Go integration 测试层未有效接入 CI，open。提醒升级需运行本项目契约测试，不能以主分支 CI 绿灯代替。
- [#18280](https://github.com/infiniflow/ragflow/issues/18280) Invoke SSRF、[#15437](https://github.com/infiniflow/ragflow/issues/15437) Markdown 图片 SSRF 均仍 open，但本地已有 URL 校验、DNS pinning 等防护；Invoke 禁用重定向，Markdown 逐跳校验。应做回归，不机械重复补丁；未做真实网络攻击验证。
- [#15171](https://github.com/infiniflow/ragflow/issues/15171) Browser 上传 URL SSRF 已于 2026-08-04 closed，排除出“未解决”清单；保留快照方便核对搜索缓存误差。

## 7. 建议实施顺序与验收标准

| 阶段 | 改动边界 | 必须通过的行为验收 |
|---|---|---|
| A：先修正确性 | Gateway run/state/replay；Retrieval 最小补丁；引用清洗 | 重试不插失败消息；同会话跨 worker 不并发污染；未终止流不成功；状态不依赖引用；空 S 不调用检索 |
| B：建立可发布基线 | Workflow 发布记录、capabilities、ACL profile、推理投影 | 同版本 DSL 不变；旧会话策略明确；无权限不能调用；原始 reasoning 不公开；生产拒绝测试 ACL |
| C：证据质量 | 父块来源、Agentic evidence IDs、冲突核验、有效检索配置 | 同文父块不串来源；不同 claim 不串证据；24V 等值不误判；矛盾证据不能被自评分覆盖 |
| D：业务扩展与性能 | 只读业务适配器、批量 scope 查询、记忆策略 | 文档与实时事实来源区分；字段/单位明确；授权撤回生效；无自由写 SQL；用测量证明延迟改善 |
| E：上游升级 | 在独立升级分支重放最小补丁 | 克隆库迁移无静默失败；父块/范围/会话/引用/权限用例全过；可回滚且核验数据兼容性 |

建议固定小型合成评测集，包含精确设备型号、跨设备追问、旧版本手册、无证据、相互矛盾参数、纯附件、撤权重放、中途断流、同会话并发、混合中英文单位和 Markdown 引用。普通 Chat、Agentic、Workflow 共用相同数据与评分，记录正确性、拒答合理性、来源归属、延迟与成本。先修上述确定性错误，再确定高档位和 Workflow 是否值得默认开放。

## 8. 本次验证、变更与限制

下表保留前轮审查/F02 实施时的执行记录；F01/F12 最新结果见本节末尾实施补充。

| 命令 | 结果及含义 |
|---|---|
| `python3 enterprise/scripts/validate_workflow_artifacts.py` | workflow-artifacts-ok；模板结构校验通过，不代表运行正确 |
| `python3 -m pytest --noconftest enterprise/tests/test_answer_split.py enterprise/tests/test_sanitize_citation_markers.py -q` | 19 passed；仅独立文本处理用例，绕开不需要的服务 conftest，没有改断言/skip/xfail |
| `python3 docs/reviews/2026-09-20-audit-probes.py` | F01/F02/F03/F04/F12 的固定行为探针及其他审查观察；使用实际函数和替代 I/O，不是数据库/服务端 E2E |
| `python3 -m pytest enterprise/tests/test_workflow_artifacts.py enterprise/tests/test_workflow_client_timeout.py -q` | collection 阻塞，缺 pytest_asyncio；环境亦缺部分应用依赖，未安装或更改运行环境 |

宿主 Python 3.14 与项目声明 >=3.13,<3.14 不一致。本次不能宣称完整测试门禁通过，也没有验证实际部署配置、数据库竞争时序、LLM 效果或真实性能。后续应在项目固定依赖的 Python 3.13 测试环境完成数据库、RAGFlow API 契约与 E2E。

本次修改包含上游 Retrieval 文件 `ragflow/agent/tools/retrieval.py`、其范围回归测试、Gateway/企业前端的 F03/F04 修正、对应登记、审查探针和本报告；**无 OpenAPI、数据库、依赖或生产 Workflow 配置变化**。F02 仍是 RF-PATCH-012 的最小可重放修正，F03/F04 不修改 RAGFlow 上游，未涉及 F07。探针中的 F01/F02/F03/F04/F12 现验证修正后的不变量，其余审查观察仍保持独立；完整测试与生产部署仍待后续环境门禁。


最终工作区检查说明：审查收尾时出现了并行修改的 ragflow/agent/canvas.py、agent/tools/base.py、api/apps/restful_apis/agent_api.py、api/db/services/canvas_service.py、rag/diagnostics.py，以及新文件 rag/workflow_diagnostics.py。这些不是本次审查所写，已保留；报告行号对应审查快照，新增诊断改动未纳入完整复审。git diff --check 通过（有既存 CRLF 提示）；Issue JSON 解析和 21/20 总数校验通过。


### F01 / F12 实施补充（2026-09-20）

本轮为两个企业层独立变更单元，详见上述变更登记。只改 Gateway、企业前端、相关测试与审查记录；没有修改上游、生产 Workflow/提示词、OpenAPI、schema、认证配置、依赖锁或部署。RF-PATCH-012/ADR-021 与 Doc1.pdf 的既有工作区改动保留，非本轮实现内容。F08 ACL 按用户要求暂缓。

| 检查 | 本轮结果 |
|---|---|
| `python3 -m pytest --noconftest enterprise/tests/test_run_expiry_source.py enterprise/tests/test_sanitize_citation_markers.py enterprise/tests/test_answer_split.py enterprise/tests/test_citation_select.py -q` | 67 passed，1 failed；失败为原有 Workflow 规范化测试导入缺 FastAPI。没有删除、skip 或 xfail 该用例；本命令整体未通过。无服务测试使用实际函数/合成资料，不能替代 PG 验收。 |
| 常规 `pytest`：`test_v2_run_expiry.py`、`test_v2_conversation_contract.py`、`test_citation_select.py` | conftest 导入缺 pytest_asyncio，collection 阻塞；两独立 PG 连接、事务回滚、Chat/Workflow JSON/SSE/历史接口验收均未执行。 |
| 前端 `npm test -- src/__tests__/CitationMarkdown.test.tsx src/__tests__/Citations.test.tsx` | 37 passed（27 组前后端共享样例、1 项 DOM 渲染、9 项既有引用 UI 测试）。 |
| `cd enterprise/web && ./node_modules/.bin/tsc -b --pretty false` | 通过。 |
| `python3 docs/reviews/2026-09-20-audit-probes.py` | 通过；F01 运行中重试不插失败消息，F12 技术正文完整保留；其余未修项继续独立观察。 |
| `python3 enterprise/scripts/validate_workflow_artifacts.py` | workflow-artifacts-ok。未改 Workflow 文件。 |

额外检查：新增文件语法编译与 `git diff --check` 通过（既存 CRLF 提示）。补入嵌套明确 ID 损坏和不明确双层数字括号用例，清洗到稳定结果；固定随机种子的 3000 组合成幂等性检查通过。

宿主 Python 3.14.4，当前缺 pytest_asyncio、FastAPI、SQLAlchemy、asyncpg，未发现 Python 3.13 或 Docker 命令。未安装替代依赖，未读取 .env 或客户资料，未访问生产。交付状态为“本地实现完成、完整环境验收待办”，不能视为生产已更新。正式集成需使用项目固定 Python 3.13 与测试 PG 重跑上述服务测试，再配套更新 Gateway/企业前端；生产部署另行执行。

### F03 / F04 实施补充（2026-09-21）

本轮先实现 Workflow 事件收集器，再接入 Gateway JSON/SSE；随后完成状态、正文、引用的独立保存与前端失败提示。修改仅在企业 Gateway、企业前端、测试、探针和登记文档；没有修改 RAGFlow 上游、生产 Workflow/提示词、OpenAPI、schema、依赖锁或部署。F05 并发治理与 F08 ACL 仍分别独立、暂缓。

| 检查 | 本轮结果 |
|---|---|
| `python3 -m py_compile enterprise/gateway/query/workflow_events.py enterprise/gateway/query/workflow_client.py enterprise/gateway/query/workflow_router.py enterprise/gateway/query/v2_router.py enterprise/gateway/query/v2_store.py` | 通过；语法级检查，不替代依赖和服务验收 |
| `python3 docs/reviews/2026-09-20-audit-probes.py` | 通过；缺终态→RUN_INTERRUPTED、failed 单调、节点错误恢复、显式无据状态及 F01/F02/F12 固定行为均由合成探针验证 |
| `python3 -m pytest -q enterprise/tests/test_workflow_events.py` | 未执行：项目 conftest 导入缺 `pytest_asyncio`；测试文件已加入，需在固定 Python 3.13 环境运行 |
| `cp enterprise/tests/test_workflow_events.py /tmp/... && PYTHONPATH=$PWD python3 -m pytest -q /tmp/...` | 20 passed；仅把无服务事件测试复制到临时目录以避开项目 conftest，不能替代仓库完整 collection |
| Gateway Chat/Workflow JSON/SSE/PG 测试 | 未完成：当前缺 FastAPI/SQLAlchemy/asyncpg 与测试 PG；不得据此宣称完整验收 |
| `cd enterprise/web && ./node_modules/.bin/vitest run src/__tests__/HarnessChat.test.tsx src/__tests__/CitationMarkdown.test.tsx src/__tests__/Citations.test.tsx src/__tests__/ErrorStates.test.tsx` | 48 passed；新增失败正文提示、F12 正文保护及既有引用 UI 回归通过 |
| `cd enterprise/web && ./node_modules/.bin/vitest run src/__tests__/HarnessChat.test.tsx src/__tests__/IntegrationHarnessPage.test.tsx -t 'preserves failed status and citations|keeps a failed partial answer'` | 2 passed、26 skipped（定向验证）；完整 IntegrationHarnessPage 仍有与本变更无关的 MSW 路由/环境失败 |
| `cd enterprise/web && ./node_modules/.bin/tsc -b --pretty false` | 通过 |

事件样例与失败部分正文/引用回放测试使用脱敏合成数据；真实 Workflow 的事件形状、断流/取消、事务回滚、跨请求重放仍是集成门禁。生产部署另行执行。


## F05 / F07 / F06 实施补记（2026-09-21）

依次实现并分项登记。F05企业schema升至9，新增CONVERSATION_BUSY/CONVERSATION_RESTART_REQUIRED两个409码；F07/F06有独立上游改动及ADR。生产Workflow导入文件、ACL、官方数据库迁移、依赖锁未改；未部署、未执行生产重解析。此前“无schema/API变化”仅描述此前F01–F04/F12批次。

本轮可执行证据：28项无服务控制流/父块/语义验证测试通过（F05 5项、F07 7项、F06 16项）；F05前端14项与TypeScript检查通过；审查探针通过。F05补充提交后停止续租、终态只发送一次的边界保护。现有F01/F03/F12纯文本和事件回归在Python3.14下运行74项，73通过，1项因缺少fastapi无法导入Workflow路由而失败。企业完整pytest收集缺pytest_asyncio，真实PG/双worker、真实引擎、Python3.13固定依赖、模型盲评未验收。前端IntegrationHarnessPage既有8项失败在未改动HEAD副本同样复现，未删断言或跳过测试。

上线前必须完成服务验收、排空旧Gateway worker、迁移重复active run预检，再配套更新Gateway/前端。父块先安全读后重建；F06须完成真实模型盲评。提交/本地测试不代表生产已修复。


## F09 / F11 实施补记（2026-09-22）

两个独立变更单元：安全过程展示；可靠回放、附件路径与确认偏好。新增企业迁移及偏好接口；RAGFlow 仅改日志/模型 tracing/空 G 附件路径，不改官方数据库、依赖锁、生产 Workflow、提示词或 ACL。F08 暂缓、F10 未处理；保留用户 Doc1.pdf 修改。旧推理/旧事实记忆未物理清理。

| 验证 | 结果 |
|---|---|
| `python3 -m unittest discover -s docs/reviews/tests -q` | 45 passed；F09 6项、F11 11项及F05/F06/F07既有28项。实际纯函数/路由和存储方法配合合成I/O；不是服务集成。 |
| 文本与事件：`pytest --noconftest test_answer_split.py test_run_expiry_source.py test_sanitize_citation_markers.py test_workflow_events.py -q` | 69 passed；命令文件均位于 enterprise/tests。 |
| 前端 PreferencePanel/HarnessChat/RunOwnership/CitationMarkdown/Citations/ErrorStates/SystemSettingsPanels | 78 passed；包含安全过程、偏好面板、引用保护、回放和系统设置回归；TypeScript 检查通过。 |
| 审查探针、Workflow模板、语法检查 | 通过；F09 探针改为验证原始推理不会公开，其他发现独立。 |
| `pytest enterprise/tests/test_preference_pg.py enterprise/tests/test_user_memory.py -q` | collection 阻塞：缺 pytest_asyncio。真实独立连接确认/回滚/worker抢占恢复测试已编写，未执行。 |
| 上游 think_log / think_timeline pytest | 项目配置缺 asyncio 插件；隔离配置后仍因缺 peewee 无法导入。新增无服务白名单/日志测试通过，不能代替完整上游环境。 |

环境仍为 Python3.14.4，无 Python3.13 / Docker；缺 FastAPI、SQLAlchemy、asyncpg 等固定依赖。本地测试不等于生产修复。待验收：真实 Chat/Workflow JSON/SSE/回放/历史；带附件的空G请求实际检索次数；Memory异步落地、重复和删除；PG迁移及两个worker。部署需先排空旧worker、迁移schema并配套更新Gateway/企业前端；本次没有部署、重解析或读取生产数据。
