# 第09课：多表覆盖与安全Join规划

> 日期：2026-08-03
> 状态：已完成

---

## 一、问题

当前系统只支持单表查询。如果用户确认的查询方案涉及分布在多张表的字段，系统无法处理。需要在不破坏现有单表链路的前提下，实现多表Join的安全判断。

## 二、根因

- QueryPlan 已预留 joins/ield_sources/	arget_grain 等字段，但从未被填充
- semantic_metadata.json 已配置表关系元数据，但没有代码使用它来规划Join路径
- etrieve_schema_node 直接交给 QueryPlanSchemaResolver，不做任何覆盖分析

## 三、方案取舍

| 候选方案 | 优点 | 缺点 | 决策 |
|---|---|---|---|
| 先判断单表能否覆盖，不能再查关系 | 思路直观 | 多了一步冗余判断 | 不采用 |
| 直接查语义元数据覆盖所有字段，单表自然成为0 Join特例 | 逻辑统一，代码更简洁 | 无 | **采用** |
| 语义元数据找不到时降级让LLM推测Join | 兜底能力强 | 安全风险，LLM可能猜错关联键 | 不采用 |

**最终方案**：优先使用 semantic_metadata.json 中已配置的关系覆盖所有字段，找不到时安全拒绝（告知缺哪些关系配置），绝不降级到LLM推测。

## 四、实现内容

### 新建文件

- services/table_coverage_analyzer.py：字段覆盖分析器，通过列向量库定位每个字段所属的物理表，返回 CoverageResult
- services/join_planner.py：安全Join规划器，BFS最小生成树算法从 semantic_metadata.json 中查找连接路径

### 修改文件

- 
odes/retrieve_schema_node.py：集成覆盖分析 → Join规划 → 单表解析的完整流程

### 核心逻辑

1. **覆盖分析**：对 confirmed_plan.fields 中的每个字段，优先在主表范围检索，再全域检索，确定字段→表的映射
2. **Join规划**：多表时用BFS构建最小连接树，仅使用 enabled=true 的关系
3. **安全拒绝**：找不到路径时明确告知缺失哪些关系，提示管理员补充配置

## 五、面试表达

> "本项目多表Join不依赖LLM推测关联键，而是通过人工审核的语义元数据驱动。系统用BFS在关系图中查找最小生成树来连接多张表，找不到路径时安全拒绝而非瞎猜。这样既保证了Join的正确性，又为后续语义层建设提供了统一的数据基础。"

## 六、2026-08-04 当前架构补充

当前 JoinPlanner 保留人工关系优先和 AI 推测开关两种模式。关系路径存在时严格使用配置边；缺少路径且 `ALLOW_AI_INFERRED_JOIN=True` 时允许 LLM 根据 Schema 推测，但最终仍必须满足白名单、只读和逐表必选过滤。Coverage Analyzer 现在尊重已锁定 field_sources，禁止执行阶段静默改变字段来源。
