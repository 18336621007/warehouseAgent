# agentTest — 换电数仓 NLQ 智能查询 Agent

基于 LangChain/LangGraph 的多 Agent 数据查询系统：用户自然语言 → 语义层/双路召回 → SQL 生成 → Hive 查询 → 结果回答。

## 架构分层

```
用户/前端 (web/server.py)
        │
        ▼
┌─ langgraph_app  Agent 编排层（生产核心）─────────────────┐
│  graphs → nodes → routers → services → tools           │
└────────────────┬───────────────────────────────────────┘
        │ 依赖 RAG 检索基建
        ▼
┌─ langchain_app  RAG 检索基建层（基于 LangChain）────────┐
│  documents → vectorstores → rag → retrievers           │
└────────────────┬───────────────────────────────────────┘
        ▼
semantic_layer（语义层）  metadata（元数据）  db/datasource/validate（连接与安全）
```

说明：`langchain_app` 与 `langgraph_app` 均基于 LangChain 生态；前者是文档/向量/BM25 检索基建，后者是 LangGraph 多 Agent 编排，前者被后者依赖。

## 目录职责

| 目录 | 职责 |
|------|------|
| config/ | 常量配置 + 白名单 metadata.yaml（单一事实源） |
| db/ | Hive/Doris 连接配置 + SQL 安全守卫（白名单/LIMIT/AST 校验） |
| datasource/ | 数据源适配（HiveDataSource） |
| metadata/ | 元数据采集、字段增强、schema 指纹、语义元数据 Provider |
| validate/ | SQL 只读/白名单校验（sql_validate.py） |
| langchain_app/ | RAG 检索基建：文档构建、FAISS 向量库、BM25、检索器、Chain |
| langgraph_app/ | LangGraph 多 Agent 主系统：graphs/nodes/prompts/routers/runtime/services/state/tools |
| semantic_layer/ | 语义层：entities/metrics/physical/semantic_models/relationships/join_contracts + Provider/Matcher |
| scripts/ | 运维/构建/同步脚本（sync_metadata、build_indexes、trace_view 等） |
| tests/ | 单元/回归测试（unittest discover） |
| tests_guardrails/ | SQL 安全守卫专项测试 |
| cache/ logs/ query_results/ | 运行时数据（gitignore） |

## 常用命令

- 启动 Web 服务：`python web/server.py`
- 全量测试：`.venv\Scripts\python.exe -m unittest discover -s agentTest/tests -p "test_*.py"`
- 元数据同步：`python -m agentTest.scripts.sync_metadata`
- 日志排查：`python agentTest/scripts/trace_view.py show <request_id>`

## 关键约定

- 白名单单一事实源：`config/metadata.yaml`（运行时读取，改动即时生效）
- 语义层独立领域：配置在 `semantic_layer/`（gitignore，运行时由 Provider 加载）
- 向量库/BM25 缓存：`langgraph_app/cache/`（gitignore）
