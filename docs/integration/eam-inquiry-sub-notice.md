# EAM 问询：用户标识（`sub`）说明（白话版）

面向：EAM 开发 / 产品 / 联调负责人  
相关总文档：[`eam-inquiry-handoff.md`](./eam-inquiry-handoff.md)

本文只讲一件事：**问询时“这个人是谁”怎么表示**。不涉及文件投喂、HMAC。

---

## 1. 一句话

每个能向知识库提问的 EAM 用户，在 JWT 里必须有一个**长期不变的用户编号**，这个字段名叫 **`sub`**。

知识库用它来：

- 认出是哪位用户；
- 只让他看自己的历史对话；
- 首次合法 JWT 访问时自动开通问询（JIT），无需事先交开通名单。

---

## 2. 什么是 `sub` 规则？

**`sub` 规则 = EAM 决定：用什么当这个用户编号，以及以后会不会变。**

**已确认（联调/正式）：`sub` = EAM 用户 id，长期不变。**

| 项 | 约定 |
|---|---|
| 取值来源 | EAM 用户 id |
| 是否会变 | **否**（换部门、改名也不改 `sub`） |
| 不要用 | 本次登录会话 ID、临时 ticket、随机数 |

**要求：同一个人，今天登录和明天登录，`sub` 必须相同。**

示例（示意）：

```json
{
  "sub": "<EAM用户id>",
  "tenant": "wp04e2e",
  "roles": ["end_user"],
  "name": "张三"
}
```

完整 ACL claims 示例见 [`eam-inquiry-handoff.md`](./eam-inquiry-handoff.md) §3.4。

---

## 3. 还要不要交「开通名单」？

**不要。**

合法 JWT 首次调用 Gateway 时，知识库会自动把 `(tenant, sub)` 写入开通表（`ext_user_map`，状态 `active`）。  
EAM 只需保证：签发正确、`sub` 稳定、ACL claims 与投喂对齐。

若某用户被知识库侧停用（`disabled`），即使 JWT 仍合法也会 `403 AUTH_USER_DISABLED`。

正常路径**不应再出现** `AUTH_USER_MAPPING_MISSING`。

---

## 4. EAM 要交给我们什么？（仅本主题）

| 交付物 | 谁准备 | 说明 |
|---|---|---|
| **`sub` 规则说明** | EAM（已确认） | 用 EAM 用户 id，不变 |
| JWT 其它 claim 字段名（若与默认不同） | EAM | 当前按默认字段名，无需改 |
| 开通名单 | **不需要** | JIT 自动开通 |

知识库侧**不会**要你们的 JWT 私钥。

---

## 5. 和投喂、公钥的区别（避免搞混）

| 事项 | 和 `sub` 的关系 |
|---|---|
| 文件投喂 HMAC | **无关**。投喂不看用户 `sub` |
| JWKS 公钥 / 验签 | 只证明“这张 JWT 是 EAM 签的”；通过后自动 JIT 开通 |
| 文档 ACL（部门/组/密级） | 与 `sub` 独立；须与投喂 metadata 对齐（方案 1） |
| RAGFlow 的 `admin@ragflow.io` | **不要**写进业务用户的 `sub` |

---

## 6. 最小验收（针对 `sub`）

1. 新用户合法 JWT（此前未出现过的 `sub`）→ 能创建会话、提问，并自动落库。  
2. 已停用用户 → `403 AUTH_USER_DISABLED`。  
3. 用户 A 的 `conversationId`，用户 B 不能访问。  
4. 同一用户两次登录 `sub` 相同 → 能续问自己的历史会话。
