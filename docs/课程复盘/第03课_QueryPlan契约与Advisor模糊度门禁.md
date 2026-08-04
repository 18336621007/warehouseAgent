# 第03课：QueryPlan 契约与 Advisor 模糊度门禁复盘

> 日期：2026-07-31  
> 课程状态：已完成  
> 复盘用途：简历项目介绍、技术面试追问、架构设计复述  
> [返回文档索引](../文档索引.md)

> 后续设计调整：本课建立的澄清/方案双 Agent 工具隔离仍然有效。后续通过结构化日志发现，“高相似候选数量强制路由”会在用户已明确口径后继续否决 `full`，因此候选数量改为仅用于观测和调优；同时 Planner → Advisor 将新增显式 `PlannerHandoffState`，避免子图 State Schema 过滤判定依据。上述两项代码改造尚待实施，本复盘正文保留当时实现背景。

## 一、本课目标

将“需求理解、方案确认、Schema 加载、SQL 执行”拆成边界明确的阶段，解决以下问题：

- Advisor 生成方案后，系统无法区分“待用户确认”和“允许执行”。
- Seeker 会根据原始问题再次向量检索，可能推翻用户已经确认的表和字段。
- 用户只回复“1”“A”或字段简称时，Planner 容易丢失上一轮候选语义。
- Planner 已判断业务口径存在歧义，Advisor 仍可能自行选择字段并锁定方案。

最终链路：

```text
Planner 识别需求与模糊度
  ├─ partial/none → Advisor clarification_agent → 检索并追问
  └─ full → Advisor plan_agent → submit_query_plan
                                    ↓
                              QueryPlan(status=locked)
                                    ↓ 用户确认
                         QueryPlan(status=confirmed)
                                    ↓
                    QueryPlanSchemaResolver 精确加载
                                    ↓
                         GenerateSQL → ExecuteSQL
```

## 二、困难一：方案形成不等于用户授权执行

### 现象

Advisor 选定表、指标、维度和时间后就写入 `confirmed_plan`，但用户实际上还没有确认整份方案。字段名称表达的是“已确认”，状态语义却是“等待确认”。

### 风险

- Planner 可能把一个刚生成的方案误认为用户已经确认。
- 后续无法可靠判断用户是在选择候选字段，还是在确认完整执行方案。
- 日志无法区分方案形成和执行授权两个阶段。

### 解决方案

保留当前共享字段 `confirmed_plan`，通过 QueryPlan 内部状态区分阶段：

```text
locked：Advisor 已形成完整方案，等待用户确认
confirmed：Planner 判断用户接受整份 locked 方案，允许 Seeker 执行
```

状态转换统一交给领域服务：

- `lock_query_plan()`：标准化 Advisor 提案，派生 `tables/fields/locked_at`。
- `confirm_query_plan()`：只接受合法 `locked` 方案，写入 `confirmed_at`。

### 设计取舍

没有立刻将 State 字段重命名为 `query_plan`，避免同时修改大量上下游代码。当前通过 `status` 明确语义，字段重命名可作为后续低风险重构。

## 三、困难二：Seeker 二次向量检索导致执行漂移

### 现象

用户已经在 Advisor 阶段确认了表和字段，但旧 Seeker 仍使用 `original_question` 查询 `enriched_faiss_index`。如果向量召回结果变化，Seeker 可能加载另一张表的 Schema。

### 根因

发现候选和执行方案没有分层：

- Planner/Advisor 的任务是通过语义检索发现和澄清候选。
- Seeker 的任务应是执行最终契约，而不是再次进行语义决策。

### 解决方案

新增 `QueryPlanSchemaResolver`，Seeker 只接受 `status=confirmed` 的方案：

```text
校验 QueryPlan
→ 限制当前只能有一张表
→ 校验 database.table
→ list_tables() 精确确认物理表
→ 拒绝跨库同名表
→ describe_table() 加载物理 Schema
→ 再次核对实际库表身份
→ 校验 confirmed_plan.fields 存在
→ 生成 schema_context
```

同时从 Graph Runtime 移除 Seeker 的通用 `retriever`，从架构上切断二次语义选表能力。

### 面试价值

这是典型的“概率性发现、确定性执行”分层：向量检索可以用于候选发现，但最终执行必须依赖经过校验的稳定契约。

## 四、困难三：简短回复缺少独立语义

### 现象

用户在 Advisor 给出候选后可能只回复：

```text
1
A
pure_new_order
第一个
```

如果只使用本轮输入检索，向量库无法知道“1”对应哪个业务候选。

### 解决方案

Planner 将以下信息组合成检索问题：

- Topic 原始问题。
- 当前 QueryPlan。
- Advisor 最近一条可见回复。
- 用户本轮真实输入。

LLM 再输出完整的 `effective_query`，例如：

```text
用户输入：1
Advisor 上轮候选：1. 纯新增订单 pure_new_order
还原需求：查询纯新增订单，指标字段为 pure_new_order
```

### 设计原则

不使用“用户输入等于 1 就选择第一个”之类关键词规则。序号含义必须结合当前 Topic 消息上下文理解。

## 五、困难四：Planner 判断模糊，Advisor 却自行选字段

### 现场日志

用户问题：

```text
昨天的新增订单有多少？
```

Planner 正确输出：

```text
completeness=partial
fields=[]
high_sim_columns=7
reason=新增订单存在 new_order、pure_new_order、really_add_order、extend_new_order 等多个口径
```

但 Advisor 同一轮调用了：

```text
search_tables
search_columns
submit_query_plan
```

并自行选择 `new_order` 锁定方案。

### 根因分析

1. Advisor 只接收了 `completeness=partial`，没有接收详细的 `planner_reason`。
2. `high_sim_columns` 和 Top-K 候选只写入日志，没有作为结构化上下文传给 Advisor。
3. Advisor Prompt 虽然要求存在歧义时追问，但 Prompt 属于软约束。
4. 图级硬校验只检查“是否调用过 search_columns”，不检查 Planner 是否仍判定为模糊。
5. `lock_query_plan()` 只能校验结构完整性，无法判断 `new_order` 是否是用户真正需要的业务口径。

## 六、为什么 Planner 的 fields 为空是正确的

当前 Planner 中的 `fields` 表示“能够唯一确定的字段”，不是 FAISS Top-K 候选。

```text
fields=[]
```

表示当前没有任何字段可以唯一映射。

日志中的：

```text
pure_new_order | new_order | really_add_order | extend_new_order
```

是检索候选，而不是已确定字段。如果把候选直接写入 `fields`，Advisor、QueryPlan 和 Seeker 就可能把候选误当成允许执行的字段。

未来多表阶段可以增加独立的结构化候选和字段覆盖分析，但当前单表上线阶段不扩大 State。

## 七、考虑过但没有采用的方案

### 方案一：只把 planner_reason 加入 Prompt

优点是改动最小，但仍依赖模型遵守提示词，无法保证不调用提交工具。

### 方案二：把 Top-K 候选直接放入 fields

会混淆“检索候选”和“已经确定的执行字段”，下游容易误用，因此没有采用。

### 方案三：新增 candidate_fields、resolved_fields、unresolved_slots

长期设计更完整，也更适合多表查询，但当前改动范围较大，不符合优先完成单表上线的目标。

### 最终方案：双 Agent 工具隔离

根据 Planner 的 `completeness` 选择绑定不同工具的 Agent：

```text
clarification_agent
  条件：partial/none
  工具：search_databases/search_tables/search_columns
  不包含：submit_query_plan

plan_agent
  条件：full
  工具：检索工具 + submit_query_plan
```

这不是要求模型“不要提交”，而是让模糊模式根本没有提交能力。

## 八、最终防护链路

### 澄清模式

```text
Planner completeness=partial/none
→ Advisor 读取 planner_reason
→ clarification_agent 检索候选
→ 使用业务语言解释歧义
→ 向用户询问最关键问题
→ 无法调用 submit_query_plan
```

### 方案模式

```text
Planner completeness=full
→ plan_agent 核对目标表字段
→ submit_query_plan
→ 图级校验是否检索过目标表字段
→ lock_query_plan
→ status=locked
```

### 执行模式

```text
用户接受完整 locked 方案
→ Planner confirm_query_plan
→ status=confirmed
→ QueryPlanSchemaResolver
→ Seeker
```

## 九、关键修改文件

| 文件 | 改造内容 |
|---|---|
| `langgraph_app/state/query_plan.py` | QueryPlan 结构和状态校验 |
| `langgraph_app/services/query_plan_service.py` | locked/confirmed 状态转换 |
| `langgraph_app/services/query_plan_schema_resolver.py` | 确认方案到物理 Schema 的精确解析 |
| `langgraph_app/nodes/planner_node.py` | 多轮需求还原、最终确认和模糊度路由 |
| `langgraph_app/graphs/advisor_graph.py` | 双 Agent 工具隔离、planner_reason 透传和方案锁定 |
| `langgraph_app/prompts/advisor_prompt.py` | Planner 模糊度门禁说明 |
| `langgraph_app/tools/submit_query_plan.py` | 完整方案提交工具 |
| `langgraph_app/nodes/retrieve_schema_node.py` | 调用 Resolver 加载 Schema |
| `langgraph_app/graphs/seeker_graph.py` | 删除旧 enrich 链路 |
| `langgraph_app/runtime/graph_runtime.py` | 移除 Seeker 通用 Retriever |

## 十、手动验收场景

### 模糊指标

```text
用户：昨天的新增订单有多少？
预期：模式=clarify，工具调用中没有 submit_query_plan，locked=False
```

### 用户选择口径

```text
用户：查询纯新增订单
预期：Planner 输出 full 和 pure_new_order，Advisor 模式=plan，生成 locked 方案
```

### 最终确认

```text
用户：这个方案可以
预期：locked → confirmed → QueryPlanSchemaResolver → Seeker
```

### 非法执行

```text
confirmed_plan 缺失、status=locked、物理表不存在或字段不存在
预期：Seeker 拒绝执行，不使用向量检索兜底
```

## 十一、当前边界

- 当前只支持单表执行，`tables` 为未来多表 Join 预留。
- `filters` 仍是字符串，因此 Resolver 暂时加载确认表完整物理 Schema。
- 跨库同名表直接拒绝，等待 MetadataProvider 支持完整库表标识。
- Planner 的检索候选尚未结构化保存，未来多表阶段再设计字段覆盖模型。
- `MemorySaver` 仍是进程内短期 Checkpointer。

## 十二、面试表达

### 30 秒版本

项目早期存在两个一致性问题：Advisor 生成方案后无法区分待确认和可执行状态，Seeker 还会再次向量检索导致表字段漂移。我设计了 QueryPlan 的 `locked → confirmed` 两阶段契约，并让 Seeker 通过 Resolver 精确加载用户确认的物理 Schema。后来日志发现 Planner 已判断字段口径模糊，但 Advisor 仍会自行选字段，因此又按 completeness 拆分双 Agent：模糊模式不绑定提交工具，完整模式才允许锁定方案，从工具权限层避免 LLM 越权。

### STAR 版本

- **Situation**：多智能体问数系统中，LLM 可能在业务口径不唯一时自行选择字段，确认方案与执行阶段也存在状态混淆。
- **Task**：保证用户确认内容、物理 Schema 和最终 SQL 一致，并阻止模糊需求提前执行。
- **Action**：设计 QueryPlan 两阶段状态、精确 Schema Resolver、Planner 多轮需求还原，以及基于 completeness 的 Advisor 双 Agent 工具隔离。
- **Result**：`partial/none` 无法调用提交工具，只有用户口径明确后才能形成 locked 方案；Seeker 只执行 confirmed 方案，不再二次语义选表。

## 十三、常见面试追问

### 为什么不只依赖 Prompt？

Prompt 是概率约束，模型仍可能违规调用工具。双 Agent 通过工具绑定实现能力隔离，属于确定性安全边界。

### 为什么不让 Seeker 在方案缺失时重新检索？

这会掩盖 Planner/Advisor 的错误，并可能执行用户没有确认的表和字段。执行层应该失败，而不是猜测。

### 为什么不把 Top-K 字段放入 fields？

Top-K 是候选集合，`fields` 是已解析或已确认字段。混用会导致下游把候选当成执行契约。未来可增加独立候选结构，但不能污染 QueryPlan。

### 未来多表查询怎么扩展？

Planner 先解析业务指标、维度和时间，表覆盖分析判断单表是否足够；不足时由语义层和确定性 Join Planner 生成物理关联方案，业务用户不直接选择 Join 条件。

## 十一、2026-08-04 当前架构补充

本课“partial 禁止提交、full 才允许提交”的硬工具隔离已经调整为 Adaptive 模式。原因是日志证明 Planner 的 completeness 只是进入 Advisor 前的初判：用户已经解决最后一个业务歧义后，Advisor 仍可能因为 Planner 返回 partial 而无法锁定方案，造成“展示方案—用户确认—系统才真正锁定—再次确认”的重复交互。

当前方案是：Advisor 始终具备提交工具，但图级仍校验目标表字段检索和 QueryPlan 完整性；存在多个有效业务口径时必须提问，歧义已经解决时同轮锁定。安全性由 Prompt、工具调用校验、QueryPlan 校验和标准化确认消息共同保障。
