# CHANGE-REQUEST：Citation `refIndex`（OpenAPI v2.8）

## 原因

EAM 需将助手正文中的 `[ID:n]` 渲染为角标，但不能把 `n` 当作 `citations[n]`。需要在 Citation DTO 上 additive 暴露与标记一致的 `refIndex`。

## 方案

- `contracts/integration-openapi-v2.yaml` → **2.8.0**，`Citation.refIndex: integer | null`
- Gateway：`select_cited_chunk_refs` + `_external_citations` / `public_citation` 回传 `refIndex`
- EAM 通知：`docs/integration/eam-inquiry-citation-marker-notice.md`

## 替代方案

仅文档约定「按首次出现顺序 = 数组下标」——易错、无法表达 overlap 回退的无绑定项。

## 兼容与回滚

- additive；忽略未知字段的客户端不受影响
- 回滚：去掉 `refIndex` 投影并降 OpenAPI 描述版本即可

## 测试

```bash
python -m pytest enterprise/tests/test_citation_select.py \
  enterprise/tests/test_external_citations_refindex.py \
  enterprise/tests/test_citation_file.py::test_public_citation_includes_ref_index_without_db -q
```
