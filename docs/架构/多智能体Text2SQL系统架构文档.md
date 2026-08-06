# 多智能体 Text2SQL 系统架构文档

> 最后更新：2026-08-05 | 指标跨轮确认闭环已完成，下一课进入连续问答与延迟澄清恢复
> [返回文档索引](../文档索引.md)

## 一、概述

本系统基于 LangGraph 构建多智能体协同框架，将用户的自然语言分析需求自动转换为 Hive SQL 并执行查询。架构对齐论文 SQL-MARS 的 Planner → Advisor/Seeker 双通道设计，并扩展了 Evaluator 评估沉淀模块实现系统自迭代。

**技术栈**：LangGraph（图编排）、LangChain（LLM 接口）、FAISS（向量检索）、PyHive（数据源连接）、MySQL（增强元数据存储 + 评估记录）、MemorySaver（进程内短期 checkpoint）、Flask（Web 服务）。

**四大智能体**：
| 智能体 | 职责 | 对应论文章节 |
|--------|------|-------------|
| **Planner** | 模糊度判定 + 路由分发 | 3.1.1 |
| **Advisor** | 模糊需求多轮澄清，三级渐进推荐 | 3.1.2 |
| **Seeker** | SQL 生成 + 校验 + 执行 + 答案格式化 | 3.1.3 |
| **Evaluator** | 多维度评分 + 优质对话沉淀 + 自迭代 | 3.1.4 |

---

## 二、整体架构

```mermaid
flowchart TD
    UI["Web/CLI：身份字段 + current_user_input"] --> Capture["capture_user_message"]
    Capture --> Planner
    Planner -->|partial/none| Ask["Advisor 检索并追问用户"]
    Ask -->|未确认口径由程序门禁拦截| Capture
    Planner -->|full或用户已解决歧义| Plan["Advisor 提交完整方案"]
    Plan -->|submit_query_plan| Locked["QueryPlan status=locked"]
    Locked -->|下一轮请求| Capture
    Planner -->|用户接受完整方案| Confirmed["QueryPlan status=confirmed"]
    Confirmed --> Seeker
    Seeker --> Resolver["QueryPlanSchemaResolver"]
    Resolver --> Generate["GenerateSQL/Validate/Execute"]
    Generate --> Evaluator
    Evaluator --> END
```

### 图结构

- **父图**（Supervisor）：记录用户消息 → Planner 路由决策 → Advisor/Seeker。
- **Advisor 统一模式**：只使用 `plan_agent`（绑定全部工具）；指标编号、字段名和中文含义优先由 pending 状态确定性解析，其余语义再交给模型。
- **同轮锁定**：`partial/none` 时先检索并追问，未确认口径由指标歧义门禁在 `submit_query_plan → lock_query_plan` 之间拦截；用户本轮解决歧义后直接提交完整方案生成 `locked`。
- **查询方案领域服务**：`lock_query_plan()` 将 Advisor 提案标准化为 `locked`；`confirm_query_plan()` 只允许 Planner 将合法 `locked` 方案升级为 `confirmed`。
- **Seeker 子图**：`retrieve_schema → generate_sql → validate_sql → execute_sql → build_final_answer → evaluator`。
- **状态隔离**：MemorySaver 以 `conversation_id:topic_id` 为 checkpoint `thread_id`，同一 Topic 多轮共享状态。
- **消息记忆**：`messages + add_messages` 统一保存用户、Advisor、工具和 Seeker 消息。

### 关键设计原则

1. **执行契约唯一**：`confirmed_plan` 是共享 State 中的唯一查询契约，具体阶段由 `status=locked/confirmed` 区分。
2. **模糊度硬门禁**：Planner 的 `completeness` 只是初判；Advisor 可以核验后提交方案，但任何 unresolved 指标都会在 `lock_query_plan` 前被程序拦截。
3. **职责分离**：Advisor 负责澄清或形成 `locked` 方案；Planner 负责理解用户是否接受完整方案；Seeker 只执行 `confirmed` 方案。
4. **不允许执行层猜测**：Seeker 不再通过向量检索重新选表选字段，方案缺失或物理 Schema 不一致时直接失败。
5. **检索证据不直接决定路由**：高相似候选数量保留为日志和调优指标，但不再作为把 `full` 强制降为 `partial` 的硬门禁；业务完整度由对话语义、元数据映射、QueryPlan 校验和用户确认共同决定。
6. **多表覆盖分析已实现**：`TableCoverageAnalyzer` + `JoinPlanner` 已集成到 `retrieve_schema_node`，BFS安全Join路径规划从 `semantic_metadata.json` 驱动，找不到关系时安全拒绝。多表 Schema 解析（加载多表结构）将在第10课实现。
7. **下一阶段功能优先**：在保留 confirmed_plan 业务契约的基础上新增 ExecutionPlan、关系元数据和确定性 Join Planner，再扩展分析性查询。

## 三、模块职责

### 3.1 State 分层设计（`state/`）

State 按“身份 → Topic → 公共流程 → 领域状态”分层：

```
state/
├── base_state.py       # IdentityState → TopicState → BaseState
├── planner_state.py    # route / planner_reason / planner_entities / confidence
├── advisor_state.py    # confirmed_plan / final_answer
├── seeker_state.py     # schema / SQL / 结果 / Evaluator 字段
└── agent_state.py      # GraphInput / GraphOutput / AgentState 聚合
```

核心规则：

- Web/CLI 只传 `conversation_id / topic_id / request_id / current_user_input`。
- `original_question / advisor_turns / confirmed_plan` 由 Graph State 管理。
- `messages` 使用 `add_messages` reducer，节点只返回本轮新增消息。
- `advisor_messages` 和 State 字段 `advisor_last_answer` 已删除；Planner 从标准消息中读取最近的 Advisor 回复。
- 子图编译时使用自己的 State，父图使用聚合后的 `AgentState`。
- 父图存在某个字段不代表子图一定能读取；跨 Agent 字段必须显式出现在子图 State Schema 中。
- 当前已发现 `AdvisorState` 缺少 `planner_entities/planner_reason`，会导致 Planner 输出进入 Advisor 后被过滤。目标方案是增加独立 `PlannerHandoffState`，由 PlannerState 和 AdvisorState 共同继承；该代码改造尚未实施。

详见 [State 与记忆系统架构](./State与记忆系统架构.md)。

---

### 3.2 Planner（`nodes/planner_node.py`）

**职责**：唯一调度中心，负责需求还原、模糊度判断、最终确认和父图路由。

核心流程：

1. 用“原始问题 + 本轮输入 + Advisor 最近回复 + 当前方案”组成检索问题，避免用户只回复“1”“A”时丢失语义。
2. 从表层和字段层 FAISS 召回候选元数据，并由 LLM 输出 `effective_query / tables / fields / completeness / accept_locked_plan`。
3. 目标设计使用 LLM 还原后的 `effective_query` 统计高相似候选数量，仅作为可观测指标记录到日志；当前代码仍存在覆盖 `completeness` 的硬分支，尚待删除。
4. `none/partial` 时进入 Advisor 自适应核验；若本轮解决全部歧义可以提交，否则由程序门禁生成候选并保持 clarifying。
5. 需求明确但尚无最终确认时路由 Advisor 的方案模式，由 Advisor 生成完整 `locked` 方案。
6. 只有 `accept_locked_plan=true` 且 `confirm_query_plan()` 校验通过时，才写回 `status=confirmed` 并路由 Seeker。

**关键边界**：

- `planner_entities` 是语义分析结果，不是执行契约。
- 用户修改部分口径时，LLM 应尽量在当前 `locked` 方案基础上修改；如果方案根本错误，也允许重新规划。
- 最终确认后严格复用 locked 方案中的表和字段，禁止新一轮向量检索覆盖用户已确认内容。

**配置项（`config/planner.py`）**：

| 参数 | 当前值 | 说明 |
|---|---:|---|
| `TABLE_SEARCH_K` | 5 | 表级召回数量 |
| `COLUMN_SEARCH_K` | 7 | 字段级召回数量 |
| `HIGH_SIMILARITY_THRESHOLD` | 0.65 | 目标定位为高相似候选观测阈值；当前硬分支待删除 |
| `MAX_HIGH_SIMILARITY_COUNT` | 3 | 目标定位为统计和告警基线；当前仍参与硬路由 |
| `EXAMPLE_SIMILARITY_THRESHOLD` | 0.7 | 优质示例最低相似度 |

### 3.3 Advisor（`graphs/advisor_graph.py`）

**职责**：根据 Planner 模糊度选择澄清模式或方案模式，避免模型在业务口径不唯一时自行选字段。

**单 Agent + 程序门禁**：

| 模式 | 触发条件 | 可用工具 | 允许结果 |
|---|---|---|---|
| `plan_agent` 检索追问 | `completeness=partial/none` | `search_databases/search_tables/search_columns` | 解释歧义并向用户追问，未确认口径被门禁拦截 |
| `plan_agent` 提交方案 | `completeness=full` 或用户本轮解决歧义 | 检索工具 + `submit_query_plan` | 提交完整 `locked` 方案 |

Advisor 统一使用 `plan_agent`；用户回复的编号、第几个、候选名称或业务口径完全由模型结合候选列表和历史判断，程序不再机械解析用户文本。

**上下文输入**：

- Planner 还原后的 `effective_query`。
- 用户本轮原始输入。
- Planner 候选表、已确定字段和 `completeness`。
- `planner_reason`，目标用于解释具体是哪一个指标、维度或表存在歧义；当前跨子图传递待 `PlannerHandoffState` 修复。
- 当前已有 `locked/confirmed` 方案。

**方案锁定流程**：

1. Planner 返回 `full`，Advisor 选择 `plan_agent`。
2. 必要时按库 → 表 → 字段逐层检索；提交方案前必须检索目标表字段。
3. 调用 `submit_query_plan` 提交完整提案。
4. 图级代码调用 `lock_query_plan()`，由程序派生 `tables/fields/status/locked_at` 并执行结构校验。
5. Advisor 将标准方案展示给用户，等待下一轮确认或局部修改。

**防幻觉机制**：

- `partial/none` 时提交工具根本不可用，模型无法产生真实 `locked` 方案。
- Prompt 要求结合 Planner 原因和检索结果，使用业务语言解释候选口径。
- 图级钩子校验目标表字段是否在工具结果中。
- `tables`、`fields`、状态和时间戳由领域服务维护，不交给 LLM 自由生成。
- Advisor 不能写入 `status=confirmed`，也不能直接路由 Seeker。

### 3.4 Seeker（`graphs/seeker_graph.py`）

**职责**：只执行最终确认的单表查询方案，完成精确 Schema 加载、SQL 生成、校验、执行和答案生成。

**当前管线**：

```text
retrieve_schema → generate_sql → validate_sql
    ├─ execute_sql → build_final_answer → evaluator
    └─ prepare_sql_fix → generate_sql
```

`retrieve_schema` 不再表示向量检索，而是调用 `QueryPlanSchemaResolver`：

1. 要求 `confirmed_plan.status == "confirmed"`。
2. 当前只允许一张表，并校验 `table == tables[0]`。
3. 要求完整 `database.table` 标识。
4. 通过 `list_tables()` 精确核对目标表，跨库同名表直接拒绝。
5. 通过 `describe_table()` 加载物理 Schema，并再次核对实际库表身份。
6. 校验 `confirmed_plan.fields` 全部存在。
7. 将确认表的完整物理 Schema 写入 `schema_context`。

当前加载完整表结构，是因为 `filters` 仍是字符串，暂时无法可靠提取所有过滤字段；这不是 Seeker 重新选字段。

**SQL 生成与一致性校验**：

- LLM 根据 `confirmed_plan + schema_context` 生成 SQL。
- 程序级校验逐项比对表、度量、维度、时间字段和过滤条件。
- 校验失败时进入修复或 fallback 逻辑，但不能改变确认方案。

**SQL 安全校验**：

- 自动追加 `LIMIT 1000`。
- AST 语法检查。
- 表名白名单和重试次数保护。

### 3.5 Evaluator（`nodes/evaluator_node.py`）

**职责**：多维度评分 + 优质对话沉淀，实现系统自迭代。

**评分指标**：
| 指标 | 类型 | 说明 |
|------|------|------|
| `time_score` | 客观 | LLM 响应耗时（归一化到 0-100） |
| `turn_score` | 客观 | `advisor_turns`（同一 Topic 的 Advisor 澄清次数） |
| `llm_self_score` | 主观 | LLM 对上下文连贯性、需求满足度的自评（0-100） |
| `user_score` | 主观 | 用户打分（1-5 星，映射到 0-100） |

**加权综合分公式**：
```
comprehensive_score = W_TIME * time_score + W_TURN * turn_score
                    + W_LLM_SELF * llm_self_score + W_USER * user_score
```

**优质对话存储**：
- `comprehensive_score >= 80` → 标记 `is_high_quality = 1`
- 生成 `example_hash`（MD5 of question + SQL），用于去重
- **MySQL**：`evaluated_dialogues` 表存储完整记录
- **FAISS**：`example_faiss_index` 存储向量化后的对话示例（`resolved_question` + `generated_sql`）
- **去重机制**：hash 相同 或 余弦相似度 > 0.95 → 跳过写入
- **用户评分联动**：用户打分变化 → 重算综合分 → `is_high_quality` 状态变化 → FAISS 自动增删
- **非阻断边界**：Evaluator 位于 FinalAnswer 之后，LLM 自评、MySQL 或 FAISS 异常记录为 `node.degraded`，返回默认评估值，不得把已经完成的查询改成失败。

**配置项（`config/evaluator.py`）**：
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `HIGH_QUALITY_THRESHOLD` | 80 | 优质对话分数线 |
| `WEIGHT_TIME` | 0.2 | 响应时间权重 |
| `WEIGHT_TURNS` | 0.2 | 交互轮次权重 |
| `WEIGHT_LLM_SELF` | 0.4 | LLM 自评权重 |
| `WEIGHT_USER` | 0.2 | 用户评分权重 |

### 3.6 状态可观测性与日志（`runtime/graph_logger.py`）

- 使用 `ContextVar` 自动附加 `conversation_id/topic_id/request_id/graph_thread_id`。
- 日志格式为 JSON Lines，文件为 `agentTest/logs/langgraph_app.jsonl`。
- 使用 `TimedRotatingFileHandler` 按天滚动，默认保留 14 天。
- 事件覆盖 `request.* / node.* / route.decided / state.changed / metric_* / plan.locked`。
- 每条事件携带 `seq / trace_id / span_id / parent_span_id`，可还原请求级树形调用链。
- 核心异常在 Web 请求边界生成 `error_id` 并记录完整堆栈，前端只展示安全提示。
- Intent Classifier、Evaluator 等可降级能力使用 `node.degraded`，不影响主查询结果。
- 树形查看工具：`python agentTest/scripts/trace_view.py show <request_id前缀>`。

具体查询命令和排障流程见 [日志使用与问题排查指南](../指南/日志使用与问题排查指南.md)。



---

## 四、数据流转

```text
Hive 表结构
  ↓ metadata_enricher（采集 + LLM 增强）
MySQL（enriched_databases / enriched_tables / enriched_columns）
  ↓ Document Builder + build_indexes
FAISS（db / table / column）
  ↓ Planner 语义识别与 completeness 判断
partial/none → Advisor 检索并追问用户（未确认口径由门禁拦截）
full/用户已解决歧义 → Advisor submit_query_plan → lock_query_plan
  ↓ lock_query_plan
QueryPlan(status=locked)
  ↓ 用户下一轮确认 + Planner confirm_query_plan
QueryPlan(status=confirmed)
  ↓ QueryPlanSchemaResolver + HiveMetadataProvider
精确 schema_context
  ↓ GenerateSQL → ValidateSQL → ExecuteSQL
查询结果
  ↓ Evaluator
MySQL evaluated_dialogues + example_faiss_index
```

执行链路与检索链路已经分离：Planner/Advisor 可以使用语义检索发现候选，Seeker 只能按最终契约精确执行。

## 五、FAISS 索引布局

所有索引统一路径：`agentTest/langgraph_app/cache/`。

| 索引 | 当前状态 | 消费场景 |
|---|---|---|
| `db_faiss_index` | 活动 | Advisor 库级引导 |
| `table_faiss_index` | 活动 | Planner 表识别、Advisor 表推荐 |
| `column_faiss_index` | 活动 | Planner 字段识别、Advisor 字段推荐 |
| `example_faiss_index` | 活动 | Planner、Advisor、GenerateSQL Few-shot |
| `enriched_faiss_index` | 旧兼容资产 | 已退出当前 Graph Runtime，不再供 Seeker 使用 |
| `schema_faiss_index` | 对比资产 | 原始 Schema 对比，不进入主链路 |

Seeker 的 `schema_context` 来自 `QueryPlanSchemaResolver + HiveMetadataProvider`，不是 FAISS 召回结果。

## 六、Web 前端（`web/`）

### 启动方式

```bash
# 安装依赖
pip install flask flask-cors

# 启动服务
python web/server.py

# 浏览器打开
http://localhost:5000
```

### 功能

| 功能 | 说明 |
|------|------|
| 🧠 意图识别 | LLM 自动区分闲聊和查询 |
| ➕ 新建对话 | 左侧栏「+ 新建对话」，每个对话使用独立 `conversation_id` |
| 📋 对话列表 | 左侧栏显示历史对话，支持删除和重命名，点击切换 |
| 🔍 思考过程 | AI 消息下方折叠面板：Planner 路由、命中表/字段、评分详情 |
| 📊 查看 SQL | AI 消息下方折叠面板：实际执行的 SQL 语句 |
| ⭐ 用户打分 | Evaluator 打分入口，1-5 星评分，提交后自动同步 FAISS |

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/conversations` | 新建对话 → `conversation_id` |
| `GET` | `/api/conversations` | 列出所有对话 |
| `PUT/DELETE` | `/api/conversations/{conversation_id}` | 重命名或删除对话 |
| `POST` | `/api/chat` | 传入 `conversation_id + message`，返回 SSE 流式结果 |
| `POST` | `/api/score` | 传入 `conversation_id` 提交用户评分（1-5） |

---

## 七、配置项汇总

| 文件 | 配置项 | 默认值 | 说明 |
|------|--------|--------|------|
| `config/planner.py` | `TABLE_SEARCH_K` | 5 | Planner FAISS 表检索数量 |
| | `COLUMN_SEARCH_K` | 7 | Planner FAISS 字段检索数量 |
| | `HIGH_SIMILARITY_THRESHOLD` | 0.65 | 高相似候选观测阈值（余弦距离换算） |
| | `MAX_HIGH_SIMILARITY_COUNT` | 3 | 目标为历史观测基线；当前强制路由分支待删除 |
| | `EXAMPLE_SIMILARITY_THRESHOLD` | 0.7 | 优质示例最低相似度 |
| `config/advisor.py` | `SEARCH_DB_K` | 3 | Advisor 库检索数量 |
| `config/advisor.py` | `SEARCH_TABLE_K` | 5 | Advisor 表检索数量 |
| | `SEARCH_COLUMN_K` | 10 | Advisor 字段检索数量 |
| | `MAX_DEMO_ADVISOR_TURNS` | 10 | 同一话题 Advisor 追问上限 |
| | `MAX_COLUMN_CHECK_RETRIES` | 3 | 缺少字段检索时的图级重试上限 |
| `config/evaluator.py` | `HIGH_QUALITY_THRESHOLD` | 80 | 优质对话分数线 |
| | `WEIGHT_TIME` | 0.2 | 响应时间权重 |
| | `WEIGHT_TURNS` | 0.2 | 交互轮次权重 |
| | `WEIGHT_LLM_SELF` | 0.4 | LLM 自评权重 |
| | `WEIGHT_USER` | 0.2 | 用户评分权重 |
| `config/settings.py` | 环境变量 | — | LLM 模型名、API Key、Embedding 模型 |

---

## 八、入口命令速查

```bash
# 元数据采集增强（Hive → MySQL，增量）
python -m agentTest.metadata.metadata_enricher

# 构建向量索引（MySQL → FAISS）
python -m agentTest.scripts.build_indexes

# 强制重建所有索引
python -m agentTest.scripts.build_indexes --force

# 查看向量库内容
python -m agentTest.scripts.view_faiss

# CLI 聊天
python -m agentTest.langgraph_app.demo

# Web 服务
python web/server.py
```

---

## 九、文件索引

```
agentTest/
├── config/                          # 全局配置
│   ├── settings.py                  # 环境变量读取
│   ├── planner.py                   # Planner 阈值
│   ├── advisor.py                   # Advisor 参数
│   └── evaluator.py                 # Evaluator 权重与阈值
├── metadata/                        # 元数据层
│   ├── metadata_enricher.py         # 离线：Hive → MySQL 增强
│   ├── mysql_store.py               # MySQL 读写 + 增量检测
│   └── hive_meta_provider.py        # Hive 原始 schema 读取
├── langchain_app/                   # RAG 基础设施
│   ├── app_builder.py               # RAG 与工具构建入口
│   ├── embeddings/
│   │   └── bailian_embeddings.py    # 百炼 Embedding（含批量切分）
│   ├── vectorstores/
│   │   ├── schema_vector_store.py   # FAISS 构建/加载/落盘
│   │   └── example_vector_store.py  # 示例向量库（去重 + 增删同步）
│   ├── documents/                   # Document 构建器
│   │   ├── enriched_db_documents.py     # 库级
│   │   ├── enriched_table_documents.py  # 表级
│   │   ├── enriched_column_documents.py # 字段级
│   │   ├── enriched_schema_documents.py # 单层混合（旧）
│   │   └── schema_documents.py          # 原始 schema
│   └── retrievers/
│       ├── schema_retriever.py      # 原始版检索器
│       └── enriched_schema_retriever.py # 增强版检索器
├── langgraph_app/                   # 多智能体框架核心
│   ├── demo.py                      # CLI 多轮交互入口
│   ├── message_utils.py             # 标准消息读取与可见对话上下文
│   ├── state/                       # 分层 State
│   │   ├── base_state.py
│   │   ├── query_plan.py             # QueryPlan 契约与校验
│   │   ├── planner_state.py
│   │   ├── advisor_state.py
│   │   ├── seeker_state.py
│   │   └── agent_state.py
│   ├── graphs/                      # 图定义
│   │   ├── supervisor_graph.py      # 父图
│   │   ├── seeker_graph.py          # Seeker 子图（含 Evaluator）
│   │   └── advisor_graph.py         # Advisor 子图（ReAct Agent）
│   ├── nodes/                       # 节点实现
│   │   ├── capture_user_message_node.py # 统一记录用户消息
│   │   ├── planner_node.py          # Planner 调度
│   │   ├── generate_sql_node.py     # SQL 生成 + 一致性校验
│   │   ├── validate_sql_node.py     # SQL 语法与安全校验（自动补 LIMIT）
│   │   ├── execute_sql_node.py      # Hive 执行
│   │   ├── retrieve_schema_node.py  # confirmed_plan 精确 Schema 加载
│   │   ├── build_final_answer_node.py # 最终答案生成
│   │   └── evaluator_node.py        # Evaluator 评估入库
│   ├── prompts/                     # LLM 提示词
│   │   ├── planner_prompt.py
│   │   ├── advisor_prompt.py
│   │   ├── sql_generation_prompt.py
│   │   └── evaluator_prompt.py
│   ├── routers/
│   │   ├── planner_router.py        # Planner → Seeker/Advisor
│   │   └── sql_router.py            # SQL 校验后路由
│   ├── tools/
│   │   ├── advisor_tools.py         # Advisor 检索工具
│   │   └── submit_query_plan.py     # Advisor 完整方案提交工具
│   ├── services/                    # 查询方案领域服务
│   │   ├── query_plan_service.py    # locked/confirmed 状态转换
│   │   └── query_plan_schema_resolver.py # 精确物理 Schema 解析
│   ├── runtime/
│   │   ├── graph_runtime.py         # 活动向量库、Provider 与 Resolver 初始化
│   │   └── graph_logger.py          # 统一日志
│   └── cache/                       # FAISS 向量索引（统一路径）
├── scripts/                         # 运维脚本
│   ├── build_indexes.py             # MySQL → FAISS 构建
│   └── view_faiss.py                # 查看 FAISS 内容
├── web/                             # Web 前端
│   ├── server.py                    # Flask API 服务
│   ├── intent_classifier.py         # LLM 意图识别
│   └── static/
│       ├── index.html               # ChatGPT 风格主页面
│       └── app.js                   # 前端交互逻辑
├── logs/                            # 运行时日志
└── docs/                            # 统一文档目录
```

相关专文：

- [State 与记忆系统架构](./State与记忆系统架构.md)
- [元数据与向量检索架构](./元数据与向量检索架构.md)

## 十六、2026-08-04 架构同步：确认协议与多表安全闭环

### 16.1 Agent 职责边界

- Planner 是唯一父图路由中心，但不再把 `completeness` 当作不可变化的最终结论。
- Advisor 使用 Adaptive 模式：先依据 Planner 初判检索元数据；若仍有业务歧义，只提一个关键问题；若本轮已经解决歧义，则同轮提交 `locked` QueryPlan。
- 只有真实存在 `status=locked` 的 QueryPlan 时，系统才向用户请求最终执行确认。
- Seeker 不理解模糊业务语义，只执行 `status=confirmed` 的方案。

### 16.2 多表执行计划

QueryPlan 当前同时承载业务方案和部分物理执行字段：

```text
tables / measures / dimensions / time_range
field_sources / table_plans
joins / target_grain / metadata_version
```

执行阶段依次完成：

1. Coverage Analyzer 校验每个字段的锁定来源。
2. JoinPlanner 根据关系元数据生成 Join 边；配置允许时可标记为 AI 推测 Join。
3. Schema Resolver 精确加载所有参与表字段。
4. SQL Generator 依据 Join、字段来源和逐表过滤计划生成 SQL。
5. 程序化校验、LLM 语义审计和 AST/资源保护共同决定是否执行。

### 16.3 全局逐表过滤规则

`agentTest/db/hive_guardrails.py` 提供：

```python
REQUIRED_FILTER_FIELDS_FOR_ALL_TABLES = [
    "pt_dt",
]
```

含义是每张参与表都必须出现自己的真实过滤，例如：

```sql
LEFT JOIN dim_trip.dim_company_snapshot_day b
  ON a.pt_platform = b.pt_platform
 AND a.company_id = b.company_id
 AND b.pt_dt = yesterday_expression
WHERE a.pt_dt = yesterday_expression
```

其中 `a.pt_dt = b.pt_dt` 仅表示字段对齐，不能替代两张表各自的日期过滤。业务过滤条件由各自 `table_plan.filters` 管理，可以不同。

### 16.4 自动修复与最终门禁

- 简单 SQL 缺少某张表必选过滤字段时，优先根据 confirmed QueryPlan 确定性重建。
- 确定性构造不支持的复杂时间或复杂 SQL，进入已有 LLM 修复循环。
- 如果修复后的 SQL 仍不满足全局过滤、Join 键、白名单或只读规则，执行前校验拒绝并进入重试/降级分支。

### 16.5 当前技术债

- 建议后续将物理字段从 QueryPlan 拆分为独立 ExecutionPlan。
- 需要为桥表、复合关系优先级、基数成本和聚合膨胀建立更完整的 Join Optimizer。
- 需要将进程内 Checkpointer 替换为持久化存储，并增加 request_id 幂等控制。

---

## 十七、2026-08-05 架构同步：AnalysisSpec 与指标口径歧义门禁

### 17.1 背景

真实日志中，用户询问“昨天新增订单最多的经销商的名称、状态、业务经理”时，Planner 已判定 `partial` 且存在 `new_order`、`really_add_order`、`pure_new_order`、`dealership_new_order` 等多个指标候选，但 Advisor 受高相似历史案例影响直接锁定 `new_order`。仅靠 Prompt 约束无法阻止模型替用户确认业务口径，因此在 `submit_query_plan → lock_query_plan` 之间新增程序级指标歧义门禁。

### 17.2 AnalysisSpec 与解析证据

- `AnalysisSpec`（`state/analysis_spec.py`）从自然语言提取 `analysis_type / metric_mentions / dimension_mentions / time_range / time_grain / filters / order_by / limit / comparison`，作为跨轮 Topic 状态保留。
- 每个指标概念携带 `ConceptResolution`：`mention / status / selected_field / selected_table / resolution_source / candidates`。
- 可信解析来源只有三种：`explicit_user`（用户明确选择字段/业务词/选项编号）、`unique_metadata`（元数据唯一候选）、`semantic_default`（正式语义默认口径，第14-15课接入）。
- 历史案例、最高相似度、ADS 优先规则和 LLM 常识选择均不能独立证明用户已确认口径。

### 17.3 指标歧义门禁

`services/metric_ambiguity_validator.py` 在 Advisor 捕获 `submit_query_plan` 后校验 LLM 提交的解析字段（必须落在真实候选或上轮已确认字段内）：

```text
Agent 调用 submit_query_plan
  ↓ 校验目标表已 search_columns（保留）
  ↓ MetricAmbiguityValidator 校验 LLM 提交字段合法性
  resolved=true  → lock_query_plan（同轮锁定，携带 concept_resolutions）
  resolved=false → 丢弃本轮提交：不生成 locked_plan，
                  程序生成候选选项（字段名+真实中文说明），只追问一个指标
```

- 候选必须来自真实元数据（向量库检索 + Planner/Advisor 结构化候选），LLM 不能编造字段。
- 只对 `measure` 类型字段判指标歧义，维度单独处理。
- 排序分数只决定候选展示顺序，不产生解析证据。
- LLM 提交的 `concept_resolutions` 必须落在真实候选或上轮已确认字段内；State 中的 `explicit_user` 选择优先，LLM 漏传不能使 resolved 状态退回 ambiguous。
- 唯一候选场景仍允许同轮锁定，不采用“所有 partial 一律禁止提交”的简单方案。

### 17.4 历史案例使用边界

- 指标未解析前，Advisor 只注入历史问题摘要，不注入历史 SQL，避免历史案例锚定业务口径。
- 指标解析完成后，历史 SQL 才允许在后续表选择或 SQL 生成阶段辅助。
- 示例库与缓存目录不做任何修改或清理。

### 17.5 关键文件

| 文件 | 职责 |
|---|---|
| `agentTest/langgraph_app/state/analysis_spec.py` | AnalysisSpec 与 ConceptResolution 定义 |
| `agentTest/langgraph_app/services/metric_ambiguity_validator.py` | 指标候选合法性与门禁校验 |
| `agentTest/langgraph_app/services/metric_clarification_service.py` | 候选固化、编号解析、状态回写和澄清文本 |
| `agentTest/langgraph_app/graphs/advisor_graph.py` | 门禁集成、澄清选项与历史案例改造 |
| `agentTest/langgraph_app/tools/advisor_tools.py` | `search_column_candidates` 结构化候选 |
| `agentTest/langgraph_app/nodes/planner_node.py` | Planner 组装 AnalysisSpec |
| `agentTest/langgraph_app/state/query_plan.py` | QueryPlan 增加 `concept_resolutions` |

### 17.6 验收场景

- “新增订单”存在多个合理指标时必须追问，不得调用 `lock_query_plan`。
- 用户回答“全量新增订单/净增订单/B类新增订单”后，本轮直接锁定对应字段。
- 用户回复选项编号时，结合上一轮候选恢复完整语义。
- 唯一指标候选不增加额外确认轮次。
- 确认消息只展示用户已确认的指标字段，候选不进入最终确认。
- 字段描述使用原始备注（如 `new_order → 新增订单数`），表描述使用表自带备注。

### 17.7 用户可见消息的确定性收敛

Advisor 给用户展示的内容统一由程序模板生成，LLM 原文不作为最终展示，避免候选混入最终确认：

- **锁定方案指标收敛**：门禁通过后，`proposed_plan["measures"]` 用已解析的 `concept_resolutions` 字段收敛，候选指标不进入 `locked_plan`。
- **确认消息不带候选**：`_build_confirmation_message` 只展示最终锁定的表、指标、维度、时间；即使锁定方案残留候选字段，展示层也会按 `concept_resolutions` 过滤，不出现编号选项或候选列表。
- **字段中文含义用原始备注**：`_extract_field_meaning` 优先“原始备注”，无原始备注时才取首个别名，避免把整串别名（如“新增订单量、新订单数、当日新增订单…”）展示给用户。
- **表中文含义用表自带备注**：确认消息中的表描述读取 `original_comment`，不使用增强后的长备注。
- **澄清选项简洁**：程序生成候选选项时只展示“一句中文含义（字段名）”，多表候选追加来源表短名，不使用 LLM 原文。
- **候选排序**：相关性降序，优秀案例命中字段仅加权排序（`EXAMPLE_FIELD_BOOST`），不产生解析证据。

### 17.8 指标歧义门禁配置（`config/advisor.py`）

| 配置 | 默认值 | 说明 |
|---|---|---|
| `MAX_AMBIGUITY_CANDIDATES` | 6 | 澄清候选数量上限 |
| `MIN_CANDIDATE_SCORE` | 0.5 | 候选相似度下限，低于该分视为不相关 |
| `EXAMPLE_FIELD_BOOST` | 0.1 | 优秀案例命中字段的排序加权，只影响展示顺序，不产生解析证据 |
## 十八、2026-08-05 架构同步：指标跨轮确认闭环

### 18.1 原循环路径

```text
Advisor 展示候选
  ↓ 用户回复“第二个”
Planner 的 effective_query 已理解目标字段
  ↓ 但 AnalysisSpec 仍保留 ambiguous
Advisor 的 concept_resolutions 又是可选参数
  ↓
MetricAmbiguityValidator 再次拦截
  ↓
用户被要求重复确认
```

根因不是模型完全没有理解，而是模型理解、程序 State 和门禁证据没有形成同一条状态转换。

### 18.2 当前闭环

```text
MetricAmbiguityValidator 生成候选
  ↓
MetricClarificationService 固化 clarification_id + options
  ↓ 写入 AnalysisSpec.pending_metric_clarification
下一轮 Planner 先确定性解析用户输入
  ↓
ConceptResolution(status=resolved, source=explicit_user)
  ↓ 清理 pending
Advisor 接收已确认指标上下文
  ↓
程序按 resolved field 收敛 measures
  ↓
lock_query_plan
```

关键变化：

- 候选不仅展示给用户，也进入 State。
- 编号语义由创建 pending 时的 options 决定，不依赖重新召回。
- Planner 基于已有 AnalysisSpec 增量更新，不再重新创建后丢失 pending。
- Validator 保留上轮真实候选中的 resolved field，候选重排不使选择失效。
- Advisor 未调用提交工具时的预校验结果也会写回 State。
- Advisor Graph 只保留编排，澄清领域逻辑迁入 `MetricClarificationService`。
- 日志 `answer_summary` 记录最终用户可见文本，而非被程序覆盖的 LLM 原始回复。

### 18.3 当前限制

当前 `pending_metric_clarification` 仍是单活动项，能够可靠处理紧接下一轮的编号，但还不能完整表达：

- 用户先询问候选差异，数轮后才选择。
- 同一 Topic 同时存在多个未解决澄清。
- 查询成功后引用“第一名”“这些经销商”“刚才结果”。
- 基于上一轮 QueryPlan 只修改时间、过滤或维度。

这些能力进入第13课。

## 十九、第13课目标架构：连续问答与延迟澄清恢复

### 19.1 不新增 Agent

连续问答是在现有状态契约上的增强，不新增 FollowUp Agent。建议使用轻量服务或节点完成：

```text
Capture
  ↓
PendingClarificationResolver
  ↓
FollowUpAnalyzer
  ↓
Planner → Advisor / Seeker
```

- `PendingClarificationResolver` 先处理可确定的编号、字段名和中文含义。
- `FollowUpAnalyzer` 判断本轮是结果追问、方案增量修改、候选解释还是新查询。
- Planner 继续负责形成完整有效需求和 AnalysisSpec。
- Advisor 继续负责元数据核验与业务澄清。
- Seeker 继续只执行 confirmed QueryPlan。

### 19.2 结果追问

查询完成后生成 `QueryResultSnapshot`，至少保存：

- `result_id / source_request_id`。
- `confirmed_plan` 和字段来源。
- 返回列、有限预览行、总行数和结果摘要。
- 可继续查询的实体键，例如 `company_id`。

用户问“第一名的业务经理电话”时，系统先从快照定位第一名实体，再基于实体键补充查询；不能只把上一轮自然语言答案拼接给模型猜测。

### 19.3 延迟澄清

单 pending 升级为 `pending_clarifications` 注册表：

- 每条记录有稳定 `clarification_id`、创建请求、候选快照和状态。
- 用户询问选项差异时保持 `open`，不能误判为选择。
- 只有一个 open pending 时，隔数轮回复“第二个”仍可直接命中。
- 多个 open pending 时，单独编号必须安全拒绝，并要求指定业务概念。
- 新 Topic 与旧 Topic 状态严格隔离。
- 过期策略必须配置化，不写死固定轮数。

### 19.4 方案增量修改

对“换成近 7 天”“再加城市”“改成净增订单”等表达，生成 `FollowUpContext`：

```json
{
  "mode": "plan_refinement",
  "referenced_result_id": "...",
  "base_plan": {},
  "changed_slots": ["time_range"],
  "resolved_question": "查询近7天……"
}
```

程序只继承未修改槽位；任何新指标仍要经过指标歧义门禁，不能因为来自上一轮计划就绕过。

### 19.5 安全边界

- 结果引用只在同一 Topic 内生效。
- 无快照、快照过期或引用不唯一时必须追问。
- 全量大结果不写入 State，只保存引用和预览。
- 消息摘要可以压缩自然语言历史，但不能替代 QueryPlan、pending 和结果快照。
- 连续问答仍需经过字段来源、Join、逐表过滤和 SQL Guardrails。
