# 第06课：Planner → Advisor 契约修复复盘

> 日期：2026-08-03  
> 课程状态：已完成  
> 复盘用途：面试中说明跨子图状态传递、安全防护设计和死代码治理  
> [返回文档索引](../文档索引.md)

## 一、本课目标

修复 Planner 和 Advisor 之间的三个正确性缺陷，为多表 Join 建立可信的状态传递基础。

## 二、困难一：Advisor 子图收不到 Planner 的判定结果

### 现象

用户选择候选口径后，Planner 已判定 `completeness=full`，但日志显示 Advisor 收到的 `completeness=none`，导致澄清模式的 Advisor 反复追问已确定的问题。

### 根因

LangGraph 子图只接收其 State Schema 中声明的字段。`AdvisorState` 没有声明 `planner_reason` 和 `planner_entities`，父图 `AgentState` 虽然聚合了 Planner 的所有字段，但进入 Advisor 子图时被过滤。

### 解决方案

新建 `PlannerHandoffState` TypedDict，包含 `planner_reason` 和 `planner_entities`。`PlannerState` 和 `AdvisorState` 共同继承该契约，确保跨子图字段不被过滤。

### 设计取舍

为什么不用 `BaseState` 传递：`BaseState` 会被 `SeekerState` 继承，而 Seeker 不需要关心 Planner 的原始判定。抽取独立契约只让 Planner 和 Advisor 感知，符合接口隔离原则。

## 三、困难二：候选数量硬路由导致用户确认后仍被追问

### 现象

日志中 `high_sim_columns=6`，超过 `MAX_HIGH_SIMILARITY_COUNT=3`。即使 Planner 的 LLM 判定需要已通过用户选择唯一化，系统仍因向量召回的高相似字段数量把 `full` 改为 `partial`。

### 根因

向量相似度是检索质量指标，不是业务歧义的裁判。用户已通过 Advisor 澄清选择了唯一字段后，其他字段的向量相似度仍然很高（因为语义天然接近），但业务歧义已经消除。

### 解决方案

删除 `has_excessive_candidates` 的计算和路由覆盖逻辑（发现时已是死代码，变量从未被后续路由使用）。候选数量继续保留在日志中作为调优和告警指标。

## 四、困难三：Plan 模式下未形成 locked 方案时伪装确认

### 现象

Advisor 的 LLM 文本声称"当前方案已完整明确"，但日志显示 `locked=False`。用户回复"好"后，`confirm_query_plan()` 报"当前不存在可确认的查询方案"。

### 根因

`advisor_graph.py` 的 `final_answer` 判断链有三个分支：有 locked_plan → 展示方案；有 plan_validation_error → 提示不完整；else → 直接展示 LLM 原文。Plan 模式下 Agent 没有调用 `submit_query_plan` 时，`plan_validation_error` 仍是空字符串，直接掉进 else 分支展示 LLM 文本。

### 解决方案

在 else 前插入 `elif can_submit_plan:` 分支。Plan 模式下未形成 locked_plan 时，返回明确错误消息，不展示 LLM 原文。

## 五、关键修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `state/planner_handoff_state.py` | 新建 | 交接契约 TypedDict |
| `state/planner_state.py` | 修改 | 继承 PlannerHandoffState，删除重复字段 |
| `state/advisor_state.py` | 修改 | 继承 PlannerHandoffState |
| `nodes/planner_node.py` | 删除死代码 | has_excessive_candidates 计算移除 |
| `graphs/advisor_graph.py` | 新增分支 | elif can_submit_plan 防护 |

## 六、面试表达

### 30 秒版本

项目里 Planner 判定请求完整后路由给 Advisor，但发现 Advisor 子图有时收不到 Planner 的判断结果，导致重复追问。根因是 LangGraph 子图的 State Schema 过滤了未声明字段。我抽取了独立的 HandoffState 契约，让 Planner 和 Advisor 共同继承，解决了跨子图字段丢失。另外移除了候选数量的硬路由（向量相似度不等于业务歧义），并增加了 Plan 模式下未锁定方案的协议防护。

### 追问：为什么不把交接字段放进 BaseState

BaseState 会被所有子图继承，包括 Seeker。Seeker 只负责执行已确认方案，不需要知道 Planner 的原始判定上下文。抽取独立契约只让 Planner 和 Advisor 感知，遵循接口隔离原则。

## 七、2026-08-04 当前架构补充

Handoff 修复后进一步完成确认协议收敛：Planner 的 accept_locked_plan 只负责把现有 locked 方案转为 confirmed；Advisor 必须在用户解决歧义的当前轮真实调用 submit_query_plan。未锁定时即使 LLM 输出完整方案，也会被标记为“仅用于继续核对”，禁止请求最终执行确认。
