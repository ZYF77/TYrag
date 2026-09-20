# F04：状态、正文与引用独立保存

状态：企业 Gateway/前端代码已实现；固定 Python 3.13、PostgreSQL、前端和 HTTP 契约验收待执行；未部署。

## 成功标准

- `completed`、`no_reliable_evidence`、`failed` 不因正文或 citations 是否为空而互相改判。
- 安全正文和授权校验后的 citations 在失败消息中与运行终态同事务保存；JSON 仍返回既有非 2xx。
- 失败 SSE 不发送 `answer.completed`，只发送 `run.failed`；此前安全正文/引用可被实时界面、历史和回放读取。
- 前端保留持久化状态和引用，失败旁显示“回答未完成，不可视为最终答案”。
- 整个上游 reference 列表中的越界引用和内部协议泄露仍独立走安全错误/清除，不以失败状态放宽授权范围。

## 变更文件与边界

- `enterprise/gateway/query/v2_store.py`：新增失败消息与运行终态原子保存方法。
- `enterprise/gateway/query/v2_router.py`、`workflow_router.py`：保留安全正文/引用，失败 JSON/SSE 走既有错误契约。
- `enterprise/web/src/components/harness/HarnessChat.tsx`、`enterprise/web/src/components/chat/MessageItem.tsx`、对应前端测试：失败内容保留并显示未完成提示。
- `enterprise/tests/test_v2_conversation_contract.py`：更新无据状态断言并增加 Chat/Workflow、JSON/SSE 失败保留测试。

未修改公开 API 字段、状态枚举、schema、认证配置、生产 Workflow、RAGFlow 上游或依赖；不处理 F01 之外的并发、租约续期和 fencing。引用清洗由 F12 独立维护。

## 验证

```sh
python3 -m pytest enterprise/tests/test_v2_conversation_contract.py -q
cd enterprise/web
npm test -- src/__tests__/HarnessChat.test.tsx src/__tests__/IntegrationHarnessPage.test.tsx
./node_modules/.bin/tsc -b --pretty false
```

当前环境缺 Gateway 服务依赖和测试 PostgreSQL，后端 HTTP/事务测试尚未执行；前端需重跑本轮新增 Harness 断言。失败保存、越界引用、历史回放和断线恢复必须在固定环境完成，不能以源码探针替代。

## 升级与回滚

前端与 Gateway 一起发布，避免旧界面把失败部分答案显示成完整答案。升级时回归 JSON/SSE/历史字段和 citation 文件票据。回滚只撤回本变更代码，不重写已有消息；历史中已保存的业务状态、正文和引用保持原样。
