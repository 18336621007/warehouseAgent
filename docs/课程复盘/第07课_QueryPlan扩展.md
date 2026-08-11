# 第07课：QueryPlan 扩展与多表预留复盘

> 日期：2026-08-03  
> 课程状态：已完成  
> 复盘用途：面试中说明 QueryPlan 契约演进、方案设计取舍和单表到多表的平滑过渡  
> [返回文档索引](../文档索引.md)

## 一、本课目标

在不破坏现有单表链路的前提下，为多表 Join 预留 QueryPlan 的物理执行字段，并让 `lock_query_plan()` 支持从字段来源推导参与表。

## 二、困难一：要不要独立拆一个 ExecutionPlan

### 讨论过程

最初方案是新建独立 `ExecutionPlan` TypedDict 和 `ExecutionPlanner` 服务，在 `retrieve_schema` 节点中先派生、再传给 Resolver。单表场景下 `ExecutionPlan` 的每个字段都能从 `confirmed_plan` 翻译得出，没有任何新信息。

### 最终取舍

取消独立 `ExecutionPlan`，直接在 `QueryPlan` 追加四个可选字段（`total=False`）：

- `joins`：多表 Join 边，单表为空
- `field_sources`：每个业务字段归属哪张物理表
- `target_grain`：查询粒度，校验 GROUP BY
- `metadata_version`：关系元数据版本

理由：代码量少一半，不新增类型和服务，不改变调用链。`_build_confirmation_message()` 按白名单展示业务字段，新增物理字段用户看不到。

## 三、困难二：locked 方案的表列表应该怎么写

### 原逻辑

`lock_query_plan()` 固定 `tables = [table]`，假设所有字段都在同一张表。

### 问题

多表场景下，字段可能分布在 fact_order 和 dim_channel。`locked` 方案里的 `tables` 应该是 `["fact_order", "dim_channel"]`，如实反映字段来源。用户有权知道涉及了哪些表。

### 解决方案

Advisor 在 `submit_query_plan` 时通过 `_field_sources` 传递每个字段的物理来源。`lock_query_plan()` 根据 `_field_sources` 的值去重生成 `tables`。当前过渡期 `_field_sources` 为空时，回退为 `[table]` 保持单表兼容。

### 设计原则

`_field_sources` 是内部临时字段，不写入最终 QueryPlan（`pop` 移除）。第8课有了关系元数据后，字段来源可以由 JoinPlanner 自动确定，不再依赖 Advisor 手动填入。

## 四、关键修改文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `state/query_plan.py` | 扩展 | 新增 joins/field_sources/target_grain/metadata_version |
| `services/query_plan_service.py` | 修改 | tables 从 _field_sources 推导，回退单表 |
| `services/query_plan_schema_resolver.py` | 回退 | resolve() 恢复单参数，从 confirmed_plan 读取 |
| `nodes/retrieve_schema_node.py` | 回退 | 移除 execution_plan 派生逻辑 |
| `state/execution_plan.py` | 删除 | 方案废弃 |
| `services/execution_planner.py` | 删除 | 方案废弃 |

## 五、面试表达

### 30 秒版本

多表 Join 需要在 QueryPlan 中承载物理执行信息。我考虑过独立拆一个 ExecutionPlan，但单表场景下完全是冗余翻译。最终选择直接在 QueryPlan 上追加四个可选字段，用 total=False 保证不填就不出现。当前单表链路零影响，多表 JoinPlanner 完成后直接写入这些字段即可。

## 六、当前架构补充

QueryPlan 的多表字段已实际投入执行：`field_sources` 用于锁定字段物理来源，`table_plans` 用于表达每张表独立的时间与业务过滤，`joins` 支持复合键，`target_grain` 保存粒度信息。当前仍计划在分析功能稳定后拆出 ExecutionPlan，避免用户确认的业务方案在执行阶段被直接补充物理字段。
