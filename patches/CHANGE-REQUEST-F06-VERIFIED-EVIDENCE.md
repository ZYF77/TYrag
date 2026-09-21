# F06 — Agentic 稳定证据和语义验证

状态：本地实现，合成测试通过；真实模型、服务端到端和固定Python3.13验收未完成；未部署。

## 上游修改与重放

- harness新增evidence.py、numeric_evidence.py、semantic_verifier.py；请求级来源/内容/版本身份，稳定整数引用，验证结构与数值检查。
- decompose/agentic编排调用统一验证器；pipeline按子问题实例化并在调用前限制scope/kb，空路由不放宽；agent/工具schema明确全局证据编号。
- planner由代码分配唯一claim_id；types新增内部required/verification。sufficiency删除自报融合，仅明确验证结果决定充分性。
- agentic_rag_graph仅medium/high/ultra最终正文先验证后投递；低档保持原路径。dialog_service的Agentic终态优先采用验证器显式状态，不改普通Chat其他路径。
- 不新增外部依赖、官方数据库字段、公开API字段或Workflow导入配置。新增验证器自身内部提示词，生产Workflow提示词不动。

升级时逐项检查：harness是否仍存在、工具返回是否带来源字段、chunk整数ID与引用映射、模型clone是否无工具、最终答案是否经过第二个外层模型重写。上游若替换harness需重评，不强行整目录覆盖。独立提交，F07父块修复先部署。

## 验证预算与降级

单验证最多6条证据，每轮8个子问题，并发2，30秒超时；上下文不足不截断问题后假装完整验证。请求内相同输入缓存。最终答案最多两次候选生成/验证，失败回退已验证句子及缺口说明，业务状态显式no_reliable_evidence。模型高置信度、畸形输出和伪造ID不能直接改变验证状态。

数值只支持明确单位换算、同设备/属性/条件的比较；缺单位、语义字段无法字面定位、跨版本不明确均unknown。召回片段虽相关但无法满足这些条件时可答率可能下降，需通过盲评度量，不默认放宽。

## 验证

`python3 -m unittest discover -s docs/reviews/tests -p test_f06_verification.py -v`：16项通过，调用真实registry/验证器及抽取的实际medium/high/ultra/Pipeline方法；模型与外部检索为合成替身。覆盖编号串位、快照不变、来源和claim边界、数值/量纲/重叠范围、数字设备号、伪造摘录/协议、超时与并发、8项轮次上限、最终草稿未通过不公开及未核验必要项的缺口说明。数值范围相交但不能相互证明时为unknown；仅明确不相交才判数值冲突。响应不增加内部证据字段。

`enterprise/tests/fixtures/f06-blind-evaluation.json`：40条合成设备题，8类×5设备。candidate的核验结果与最终问题可回答性分开标注：例如否定错误电压假设可得到正确完整回答，不能把这种纠错一律计为误判充分。

真实盲评未执行。执行方法：在获授权的测试环境导入fixture合成资料，以simple/medium/high/ultra各运行40题；随机匿名化给人工评阅，记录每题是否回答正确、逐引用正确数、内部充分性结果、耗时和额外调用数。评阅后生成160条JSONL，再运行：
```
python3 enterprise/scripts/score_f06_evaluation.py --reviews <已独立评阅的JSONL>
```
记录字段见脚本docstring。缺项、重复项、缺少评阅数据会拒绝生成验收报告；不保存真实用户问题/模型原文，不以stub产出伪造盲评成绩。真实服务与原始事件/引用契约验收通过前不可部署。
