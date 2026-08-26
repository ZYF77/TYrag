# 问答不准：优先 RAGFlow 聊天提示词

- 状态：Accepted（S4 / B1 提示词已落地，2026-08-26）
- 日期：2026-08-26
- 决策人：Retrieval

## 原则

范围对了、检索也进了正确 `doc_ids`，但**答案主语/字段配错**时：

1. **先改 RAGFlow 成答提示词**（企业 Chat system + Agentic `FINAL_ANSWER_SYSTEM`）。
2. 不先改 Gateway scope、不先改检索 query、不先改上游检索/narrow。
3. 提示词复测仍稳定失败，再考虑成答前硬校验（上游补丁），仍不拿 Gateway 当「型号对不对」的主闸。

Gateway 只负责设备范围（`doc_ids` / 点名换绑 / 未知号 fail-closed）。**型号、出厂编号、整机 vs 部件**是生成质量，不是 scope。

---

## S4 — 问 XT30D 出厂号，答成绑定机自己的出厂号

| 项 | 内容 |
|---|---|
| conversationId | `57e42b72-b833-44ed-b41b-1ab9cf93b740` |
| 绑定 | `GQ01250017` |
| 档位 | **medium**（Agentic：Formalize → Hybrid → Composing） |
| 问题 | `XT30D 的出厂编号是多少？` |
| 实际回答 | `XT30D 的出厂编号是 F252904` |
| 引用 | `SKM_C65826081213260.pdf`（`FAC-2676-ATT-72`） |

### 不是什么

- **不是** scope 串台：`entity_scope=["GQ01250017"]`，5 份 `doc_ids` 全是 017。
- **不是** 引用了 024 的文件：该 PDF 的 `equipment_id` 就是 `GQ01250017`。
- **不是** 「F252904 不是出厂编号」：页上字段就是「出厂编号 F252904」。
- **不是** ultra 漏修：本轮是 medium；D2/narrow 修的是「DeviceCode 不当正文必现」，与本条无关。
- **不是** 答出了 024 真值 `250606`（那才叫跨设备泄漏）。

### 根因

模型把问题看残了：

- 完整约束 = **型号 XT30D** + **出厂编号**
- 实际只用了后半段「出厂编号」，在 017 文档里找到 `F252904` 就硬答
- 本范围资料**没有**「XT30D ↔ F252904」这对关系；XT30D 属于另一台 `GQ01250024`

规则「仅型号不切设备」已守住；缺口是 **主语（型号）未在证据中出现时仍嫁接本机其它编号字段**。

---

## B1-low — 把控制器型号当成整机型号

| 项 | 内容 |
|---|---|
| 初测 conversationId | `a99605cf-5fae-424a-9df2-2692dcd10e96` |
| 复测 conversationId | `21462e35-b9f7-43c7-b597-f47bc54b6910` |
| 绑定 | `GQ01250024` |
| 档位 | **low**（仍走 Agentic `direct_search`，不是 simple） |
| 问题 | 结合合格证/说明书，总结关键身份（厂家、型号、编号），并列控制器/使用安全注意事项 |
| 实际问题点 | 「设备型号」写成 **SX165006A（洗脱机控制器）**；厂家/出厂号漏掉；合格证上的 **XT30D / 250606** 未作为整机身份 |

### 不是什么

- **不是** 越权或空 scope：文档都在 024 名下（合格证 `FAC-2683-ATT-82`、控制器说明书 `FAC-2683-ATT-84`）。
- **不是** 秒拒/短路：同设备 medium/high 能答出海华机械与整机信息。

### 根因

同设备多附件时，生成侧**没按问题层次选对字段**：

- 问「该设备身份 / 型号」应优先 **整机合格证**（XT30D）
- low 档检索+成答更短，更容易抓住说明书里的 **控制器型号 SX165006A** 当整机型号

与 S4 同类：**证据里有编号/型号字样，但不是用户问的那个主语。**

---

## 提示词路线：方案与思路

### 落点（两条，缺一不可）

| 档位 | 成答真正吃的 prompt | 文件 |
|---|---|---|
| simple（推理强度 0） | 企业 Chat `prompt_config.system` | [`enterprise/gateway/query/enterprise_prompt.py`](../../enterprise/gateway/query/enterprise_prompt.py) → 写入 RAGFlow Chat |
| low / medium / high / ultra | Agentic `formalize_answer` 的 `FINAL_ANSWER_SYSTEM` | [`ragflow/rag/advanced_rag/harness/prompts/report_prompt.py`](../../ragflow/rag/advanced_rag/harness/prompts/report_prompt.py) |

S4、B1-low **都不是 simple**。只改企业 Chat、不改 `FINAL_ANSWER_SYSTEM`，这两条复测大概率仍翻车。

身份块（`scope_identity_prompt.py`）继续只解决「DeviceCode 不在正文」；**不解决**型号嫁接、整机 vs 部件。

### 要写进成答规则的约束（中文，与现网身份块同风格）

1. **主语完整性**：用户点名了型号 / 部件（如 XT30D、控制器），证据必须**显式出现该主语**（或明确写「该设备/该合格证产品型号为 …」），才允许把出厂编号、厂家等字段安到这个主语上。
2. **禁止字段嫁接**：证据里有「出厂编号 = F252904」但没有 XT30D，不得答「XT30D 的出厂编号是 F252904」。应拒答：当前范围内没有该型号 / 无法提供该型号的出厂编号。
3. **整机优先于部件**（B1）：用户问「该设备型号 / 身份」且未限定「控制器」时，优先合格证/整机铭牌上的产品型号；控制器说明书型号必须标成控制器型号，不得冒充整机。
4. **范围不切换**：问题里只有型号、没有设备号，不得暗示已查到其它设备；本范围对不上就未知，不要为了凑答去联想。

### 思路（为什么先 prompt）

- Gateway 没有权威「本机产品型号」表，无法在检索前判定 XT30D ∉ 017。
- 检索命中「出厂编号」字段在相关性上是合理的；错在**组合**，适合用生成规则卡住。
- 改动面小：Chat prompt + 一处 Agentic 成答 system；不污染 Dense/BM25，不扩大 `doc_ids`。

### 明确不做（本条）

- Gateway 因「问了 XT30D」清空或换绑 scope。
- 把型号写入 chunk / `important_kwd`。
- 为 S4 再改 `_narrow_by_keywords`。

### 复测不够再升级

若 medium S4、low B1 仍硬答：在 `formalize_answer` **成答前**做轻量校验——问题中的型号 token 未出现在本轮 evidence 文本则强制拒答。那是上游补丁，需 ADR + 单测；仍排在提示词之后。

---

## 分层对照（避免再走错层）

| 现象 | 层 | 例 |
|---|---|---|
| 未知设备号仍搜绑定机 / 型号被当设备号秒拒 | Gateway | A3/F2、S3 cue |
| DeviceCode 不在正文导致拒匹配 | 身份块 + metadata | D2 |
| 范围内答错主语/字段 | **成答提示词** | **S4、B1** |
| high 把台账号当正文关键词滤成 0 | search narrow | high D2 |

---

## 验证

- S4：绑 017，问「XT30D 的出厂编号」→ 拒答或明确本范围无该型号；**不得**答 F252904。
- B1-low：绑 024，问设备身份型号 → 整机 **XT30D**；SX165006A 若出现须标明控制器。
- 回归：D2（范围内认 DeviceCode）、A3/F2（型号不秒拒）、仅型号不切到 024。

## 回滚

- 撤回企业 prompt 新增条款并升 marker；撤回 `FINAL_ANSWER_SYSTEM` 中文规则段。
- 不回滚 Gateway `doc_ids` 与 scoped narrow。
