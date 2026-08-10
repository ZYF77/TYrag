# CHANGE-REQUEST：RAGFlow 外部文件票据解析

## 原因

`v0.26.4` 默认解析链路把 `Document.location` 当作对象存储坐标。为了满足“原始 PDF 不进入 MinIO/RAGFlow 持久存储”，需要让 `source_type=enterprise_file_share` 的虚拟文档在解析时调用 Gateway 的一次性票据接口。

## 最小改动

1. RAGFlow REST 增加虚拟外部文档注册接口和换票接口，复用 `Document.type=VIRTUAL`、`Document.location`、现有 File/File2Document 关系。
2. 两个解析执行器在 `location` 以 `external://` 开头时调用 `rag.utils.external_source.fetch_external_source`；其他文档继续走 `STORAGE_IMPL`。
3. 外部读取使用临时目录和 SHA-256 校验，不写入 RAGFlow 对象存储。

## 契约与兼容性

- 不新增官方表字段，不修改官方迁移。
- 不改变 v2 上传、解析或对象存储行为。
- 票据 URL、Gateway 地址和内部密钥均通过部署环境变量注入。

## 回滚

撤销本 CR 的 RAGFlow 文件和 Gateway v3 注册即可；既有 v2 文档不受影响。升级上游时按本文件逐项重放，不复制或重排官方目录。
