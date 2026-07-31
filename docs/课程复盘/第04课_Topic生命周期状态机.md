# 第04课：Topic 生命周期状态机复盘

> 日期：2026-07-31  
> 课程状态：已完成  
> 复盘用途：面试中说明状态机设计、流程解耦和失败边界  
> [返回文档索引](../文档索引.md)

## 一、本课目标

正式启用已经定义但尚未写入的 `topic_status`，让一次问数任务的状态不再通过 `route`、节点名称或前端临时变量推断。

目标状态：

```text
new → clarifying → confirmed → generating_sql
    → validating_sql → executing → completed
                              └→ failed
```

`cancelled` 继续保留，等待后续增加取消接口时启用。

## 二、当前发现的困难

### 困难一：topic_status 只有类型，没有运行时写入

`TopicState` 和 `GraphOutput` 已声明 `topic_status`，但当前没有节点返回该字段，因此 checkpoint 中无法查询 Topic 的真实阶段。

### 困难二：Web 和 CLI 使用 route 判断 Topic 是否结束

当前 Web 使用：

```python
if route != "advisor":
    session["_new_topic"] = True
```

这把“本轮路由到了哪个 Agent”和“整个 Topic 是否已经结束”混为一件事。未来增加失败恢复、取消、异步执行或更多 Agent 后，`route` 不能继续承担生命周期职责。

### 困难三：节点执行状态与节点完成状态不同

LangGraph 节点返回 State 时，节点本身已经执行完。因此本课采用“当前节点完成后，写入下一个业务阶段”的方式：

```text
Planner 确认方案完成 → confirmed
Schema 加载完成 → generating_sql
SQL 生成完成 → validating_sql
SQL 校验通过 → executing
最终回答完成 → completed/failed
```

这样下一个节点开始前，checkpoint 已经保存正确阶段，不需要增加专门的状态节点。

### 困难四：Evaluator 不应决定查询是否完成

Evaluator 是结果后的评估和经验沉淀，不属于核心查询成功条件。Topic 应在最终回答构建完成时进入 `completed` 或 `failed`，避免评估服务异常把已经成功返回的数据查询标记为失败。

## 三、当前设计取舍

- 不新增 `planning`、`loading_schema`、`answering`、`awaiting_confirmation` 等细粒度状态，避免状态数量膨胀。
- `clarifying` 同时覆盖普通澄清和 locked 方案等待最终确认，具体方案阶段由 `confirmed_plan.status` 区分。
- 不增加独立状态节点，直接在现有节点返回值中写入状态。
- Web 和 CLI 后续只使用终态 `completed/failed/cancelled` 判断是否创建新 Topic。
- 未捕获的基础设施异常如何统一写入 `failed`，留到后续异常处理和可观测性课程完成。

## 四、计划修改文件

- `langgraph_app/nodes/capture_user_message_node.py`
- `langgraph_app/nodes/planner_node.py`
- `langgraph_app/graphs/advisor_graph.py`
- `langgraph_app/nodes/retrieve_schema_node.py`
- `langgraph_app/nodes/generate_sql_node.py`
- `langgraph_app/nodes/prepare_sql_fix_node.py`
- `langgraph_app/nodes/validate_sql_node.py`
- `langgraph_app/nodes/execute_sql_node.py`
- `langgraph_app/nodes/build_final_answer_node.py`
- `web/server.py`
- `langgraph_app/demo.py`

## 五、最终实现

状态由现有节点直接写入，不新增状态节点：

| 节点 | 写入状态 |
|---|---|
| Capture 首轮 | `new` |
| Planner 路由 Advisor | `clarifying` |
| Planner 路由 Seeker | `confirmed` |
| Advisor 返回 | `clarifying` |
| RetrieveSchema 完成 | `generating_sql` |
| GenerateSQL 完成 | `validating_sql` |
| PrepareSQLFix 完成 | `generating_sql` |
| ValidateSQL 通过 | `executing` |
| FinalAnswer 成功或空结果 | `completed` |
| FinalAnswer 处理最终 SQL 失败 | `failed` |

Web 和 CLI 不再使用 `route` 判断 Topic 是否结束，而是使用：

```python
topic_status in (
    "completed",
    "failed",
    "cancelled",
)
```

## 六、代码检查结果

- 所有预期节点均已写入对应状态。
- SQL 修复链路可以在 `validating_sql → generating_sql → validating_sql` 之间转换。
- `_build_answer_update()` 的失败、空结果和成功调用均已传入终态。
- Web SSE 已返回 `topic_status`。
- Web/CLI 已改用生命周期状态管理新 Topic。
- 相关 Python 文件已通过 `py_compile` 静态语法检查。

## 七、发现但暂不阻断的问题

- Web 和 CLI 中原来的 `route` 局部变量已不再使用，可以后续顺手删除。
- 未捕获的 LLM、Hive、Evaluator 等异常仍会直接抛出，无法保证 checkpoint 写入 `failed`。
- Evaluator 异常可能发生在 FinalAnswer 已写入 `completed` 之后，需要在后续异常边界课程明确“查询成功”和“评估失败”的独立状态。
- `cancelled` 尚无 API 和图节点支持。

这些问题不阻塞进入可观测性课程，其中统一异常落盘会在后续异常处理阶段解决。

## 八、面试表达

### 30 秒版本

项目最初虽然定义了 `topic_status`，但运行时没有节点写入，Web 和 CLI 只能通过 Planner 的 route 猜测一次查询是否结束。我没有增加大量状态节点，而是让现有节点在完成后写入下一业务阶段，例如 Schema 加载后写 `generating_sql`，SQL 生成后写 `validating_sql`，最终回答后写 `completed/failed`。这样 checkpoint 成为生命周期真相来源，前端、恢复和监控不再依赖具体 Agent 路由。

### 为什么不增加 awaiting_confirmation？

`clarifying` 表示 Topic 正在等待用户输入，具体是普通澄清还是 locked 方案确认，由 `confirmed_plan.status` 区分，避免重复状态表达同一事实。

### 为什么 completed 不由 Evaluator 写？

Evaluator 是结果后的评估与经验沉淀，不属于查询核心成功条件。即使评估服务失败，已经成功执行并生成答案的查询也不应该被改成失败。
