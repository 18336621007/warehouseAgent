# Planner 检索阈值与观测指标校准指南

> 最后更新：2026-08-04
> 设计状态：已决定取消候选数量硬路由，代码分支尚待删除  
> [返回文档索引](../文档索引.md)

## 一、设计结论

Planner 的表级和字段级向量检索用于向 LLM 提供元数据证据，并帮助研发人员观察召回质量。

以下两个配置继续保留：

```python
HIGH_SIMILARITY_THRESHOLD = 0.65
MAX_HIGH_SIMILARITY_COUNT = 3
```

但它们的定位调整为：

```text
检索观测指标
而不是
业务完整度硬门禁
```

高相似候选数量不再直接把 LLM 已判定的 `completeness=full` 改成 `partial`。

## 二、为什么删除候选数量硬规则

候选数量只能说明向量空间中存在多个相关项，不能直接证明用户业务需求仍然模糊。

例如用户询问“回流订单”，Advisor 给出多个候选后，用户明确选择 `reflow_addition_order`。即使用户已经解决歧义，以下字段仍可能同时具有较高相似度：

```text
reflow_addition_order
extend_reflow_new_order
extend_reflow_old_order
reflow_addition_month_order
```

如果程序只按高相似字段数量判断，用户选择后仍会被强制退回 Advisor，形成重复追问。

因此需要区分：

- **检索相关性**：向量库返回了哪些相近元数据。
- **业务歧义**：结合对话后，用户是否仍未明确业务口径。

前者是证据，后者才决定是否澄清。

## 三、调整后的门禁链路

```text
用户问题和对话上下文
    ↓
表级、字段级向量召回
    ↓
Planner LLM 判断 full/partial/none
    ↓
partial/none → Advisor 检索并追问用户（门禁拦截未确认口径）
full/已解决歧义 → Advisor submit_query_plan + lock_query_plan
    ↓
search_columns 核对真实字段
    ↓
submit_query_plan + lock_query_plan
    ↓
用户确认
    ↓
confirm_query_plan
    ↓
Seeker 精确执行
```

安全性不再依赖候选数量硬规则，而由以下机制保证：

1. Planner 结构化输出。
2. Advisor 澄清模式和方案模式的工具隔离。
3. 目标表字段检索校验。
4. QueryPlan 结构校验。
5. `locked → confirmed` 两阶段确认。
6. Seeker 只执行 `confirmed` 方案。

## 四、配置项的新含义

| 配置 | 当前值 | 调整后用途 |
|---|---:|---|
| `TABLE_SEARCH_K` | 5 | 提供给 Planner 的表级候选数量 |
| `COLUMN_SEARCH_K` | 7 | 提供给 Planner 的字段级候选数量 |
| `HIGH_SIMILARITY_THRESHOLD` | 0.65 | 统计高相似候选的观测阈值 |
| `MAX_HIGH_SIMILARITY_COUNT` | 3 | 历史告警基线，用于识别候选过宽场景 |
| `EXAMPLE_SIMILARITY_THRESHOLD` | 0.7 | 历史优质示例最低采用相似度 |

`MAX_HIGH_SIMILARITY_COUNT` 后续可以重命名为更符合观测语义的名称，例如：

```python
HIGH_SIMILARITY_COUNT_ALERT_THRESHOLD = 3
```

在代码尚未重命名前，文档统一按“观测基线”理解。

## 五、日志字段

Planner 完成日志保留：

```text
high_sim_tables
high_sim_columns
completeness
route
reason
```

示例：

```json
{
  "event": "node.completed",
  "node": "planner",
  "completeness": "full",
  "high_sim_tables": 0,
  "high_sim_columns": 6,
  "route": "advisor"
}
```

这个结果不矛盾：

- `high_sim_columns=6` 表示存在多个语义相关字段。
- `completeness=full` 表示结合对话后，用户已经唯一确定所需字段。
- `route=advisor` 表示还需要 Advisor 形成并展示 `locked` 方案，而不是继续澄清。

日志查询方法见 [日志使用与问题排查指南](./日志使用与问题排查指南.md)。

## 六、应该观察哪些指标

### 6.1 澄清率

```text
Advisor澄清请求数 / 全部问数请求数
```

过高可能表示：

- 元数据描述不完整。
- Planner Prompt 过度保守。
- 同义词和业务知识缺失。
- 检索候选噪声过大。

### 6.2 重复追问率

```text
用户已经选择具体候选后，Advisor再次询问相同口径的比例
```

该指标比“高相似候选数量”更能反映真实交互问题。

### 6.3 locked 方案形成率

```text
Advisor plan模式中 locked=True 的请求数
/
Advisor plan模式请求数
```

如果 Planner 输出 `full`，但 Advisor 经常 `locked=False`，应检查：

- Planner→Advisor State 字段是否正确透传。
- Plan Agent 是否真正绑定了 `submit_query_plan`。
- Agent 是否调用了 `search_columns`。
- 工具协议重试是否生效。

### 6.4 首次方案接受率

```text
用户第一次看到 locked 方案后直接确认的比例
```

该指标反映方案质量和业务语言表达能力。

### 6.5 检索覆盖率

检查成功问数中，真实目标表和字段是否出现在 Top-K 候选中。

如果正确目标经常不在 Top-K，应调整：

- 元数据描述。
- Embedding 模型。
- 检索问题构造。
- `TABLE_SEARCH_K/COLUMN_SEARCH_K`。

不应该通过增加硬门禁解决召回缺失。

## 七、调参方法

### 7.1 调整 Top-K

提高 Top-K：

- 优点：降低目标元数据漏召回概率。
- 风险：Prompt 更长，噪声更多。

降低 Top-K：

- 优点：上下文更短、更聚焦。
- 风险：可能漏掉正确口径。

建议先分析成功和失败请求的日志，再调整 Top-K，不根据单个案例修改。

### 7.2 调整 HIGH_SIMILARITY_THRESHOLD

阈值降低会让更多候选被计入高相似统计；阈值提高会让统计更加严格。

调整后只影响观测数据和告警，不应改变业务路由。

### 7.3 调整告警基线

当 `high_sim_columns` 长期高于基线时，应排查：

- 字段描述是否过于相同。
- 是否缺少业务口径说明。
- 字段粒度和业务域是否需要加入检索过滤。
- 是否应该先锁定目标表，再统计表内字段候选。

告警的作用是提醒研发优化元数据，不是自动否决用户已经明确的选择。

## 八、代码待办

当前 `planner_node.py` 仍存在类似逻辑：

```python
elif has_excessive_candidates:
    completeness = "partial"
    route = "advisor"
```

下一次代码改造需要：

1. 删除该分支对 `completeness` 和路由的覆盖。
2. 保留高相似候选数量计算。
3. 保留 `high_sim_tables/high_sim_columns` 日志。
4. 根据需要将 `MAX_HIGH_SIMILARITY_COUNT` 重命名为告警阈值。
5. 同步修复 Planner→Advisor Handoff State，避免 Planner 的 `full` 在子图入口丢失。

## 九、验收场景

### 场景一：首次问题确实模糊

用户：

```text
昨天新增订单多少？
```

Planner 无法唯一确定指标字段，应返回 `partial`，进入 Advisor 澄清。

### 场景二：用户选择具体口径

Advisor 给出多个指标，用户选择其中一个。

Planner 能结合上下文还原唯一字段时应返回 `full`。即使日志中的 `high_sim_columns` 大于告警基线，也不应再次询问相同口径。

### 场景三：向量候选很多但业务问题明确

用户直接使用准确业务名或字段名，且目标表字段能够唯一映射。

候选数量只记录日志，不改变 `full`。

### 场景四：LLM错误判断为full

即使 Planner 误判，Advisor 仍必须：

- 检索目标表字段。
- 形成完整 QueryPlan。
- 通过领域校验。
- 展示方案并等待用户最终确认。

因此删除候选数量硬规则不会让请求直接绕过 Advisor 和用户确认进入 Seeker。

## 十、面试表达

项目早期使用“高相似候选数超过阈值就强制澄清”的硬规则保护 Text2SQL，但线上日志发现用户明确选择字段后，其他同类字段仍会保持较高向量相似度，导致重复追问。我将候选数量从业务门禁调整为可观测指标：向量检索负责提供证据，Planner 负责结合对话判断完整度，Advisor 和 QueryPlan 负责领域校验，用户确认负责最终授权。这样既保留检索质量监控，又避免把向量空间中的相关性误当成业务歧义。

## 十、2026-08-04 当前校准策略

### 10.1 completeness 只做初步判断

当前不再使用候选数量决定硬路由。Planner 的 `full/partial/none` 用于描述进入 Advisor 前的理解程度，Advisor 可以在本轮工具检索后更新事实状态：

- 仍有多个有效业务口径：继续澄清。
- 用户已回答上一轮关键问题，剩余字段唯一：同轮提交 QueryPlan。
- Planner 判 full 但 Advisor 未调用 `submit_query_plan`：视为协议异常，不能展示伪锁定方案。

### 10.2 推荐观测指标

- `planner.completeness` 分布。
- Advisor 平均澄清轮次。
- 从用户解决最后一个歧义到生成 locked_plan 的额外轮次，目标值为 0。
- locked_plan 到 confirmed 的轮次，正常值为 1。
- SQL 首次满足逐表过滤规则的比例。
- 确定性过滤修复触发率和成功率。

### 10.3 当前阈值定位

向量相似度用于候选排序、日志分析和离线调参，不直接覆盖 LLM 与程序契约判断。调优时应优先分析误召回、字段注释质量和业务同义词，而不是通过提高阈值掩盖元数据缺陷。
## 2026-08-07 pending 选择优先级（模型判断 + 白名单校验）

Planner 的相似度阈值、候选数量和 `completeness` 不能覆盖已保存的 pending 选择：

1. 用户本轮输入与 `pending_clarifications` 一起进入 Planner LLM，由模型结合【对话历史】+【待澄清候选】判断 `user_selection`。
2. 程序 `validate_user_selection` 白名单校验：`field` 逐字命中 options、`clarification_id` 对齐，通过后生成 `explicit_user`。
3. 模型未选择（询问解释、闲聊、补充条件）时保持 open pending，不视为已选择。
4. 后续召回顺序变化只能影响新候选，不能改变已展示 options 的编号语义。
5. 多个 pending 同时存在时，短编号缺少唯一性，应进入澄清而不是用最高相似度猜测（第二阶段）。

第13课连续问答落地后，该原则扩展到所有 `PendingClarification` 类型。
