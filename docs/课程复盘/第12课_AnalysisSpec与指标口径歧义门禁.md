# 第12课：AnalysisSpec 与指标口径歧义门禁复盘

> 日期：2026-08-05
> 课程状态：已完成
> 复盘用途：面试中说明 Agent 程序级安全边界、非确定性模型输出的确定性收敛和口径治理
> [返回文档索引](../文档索引.md)

## 一、本课目标

在 `submit_query_plan → lock_query_plan` 之间建立程序级指标歧义门禁：所有存在多个合理候选的业务指标，必须经过用户选择或正式默认口径解析后才能进入 `locked_plan`；用户本轮解决歧义后，Advisor 必须同轮锁定，不引入重复确认。

## 二、问题与根因

### 现象

用户询问“查询昨天新增订单最多的经销商的名称，状态，业务经理”。Planner 已正确判定 `completeness=partial`，并检索到 `new_order`、`really_add_order`、`pure_new_order`、`dealership_new_order` 等多个指标候选。但 Advisor 第一轮注入的高相似历史案例（历史 SQL 使用 `new_order`）锚定了模型，Advisor 未追问口径直接调用 `submit_query_plan` 锁定了 `new_order`。

### 根因

1. `can_submit_plan=True`，adaptive/partial 模式仍持有 `submit_query_plan` 工具。
2. 程序门禁只校验“目标表是否调用过 `search_columns`”，不校验指标是否仍有多个业务口径。
3. `lock_query_plan` 和 `validate_query_plan` 只做结构校验，不做指标解析证据校验。
4. Prompt 已要求“多口径必须追问”，但缺少程序级硬门禁，模型可以绕过。
5. Advisor 首轮注入了历史案例的原始 SQL，历史案例被当成当前用户口径证据。

## 三、方案取舍

| 候选方案 | 优点 | 缺点 | 决策 |
|---|---|---|---|
| 继续堆叠 Prompt 要求追问 | 改动小 | 模型仍可能绕过，历史案例锚定无法消除 | 不采用 |
| 所有 `partial` 一律禁止提交 | 简单 | 破坏唯一候选同轮锁定能力，增加重复确认 | 不采用 |
| 只为“新增订单”硬编码特判 | 验收快 | 无法泛化到其他业务指标 | 不采用 |
| 程序级指标歧义门禁 + 解析证据 | 可审计、可测试、可泛化 | 需要新增状态与服务 | **采用** |

**最终方案**：新增 `AnalysisSpec` 与 `MetricAmbiguityValidator`。门禁只接受三种程序可验证的解析证据：`explicit_user`（用户明确选择）、`unique_metadata`（唯一候选）、`semantic_default`（正式语义默认，后续课程接入）。历史案例和最高相似度只能参与候选排序，不能产生解析证据。

## 四、实现内容

### 新增文件

- `state/analysis_spec.py`：`AnalysisSpec` + `ConceptResolution` TypedDict。
- `services/metric_ambiguity_validator.py`：候选收集、用户选择匹配、编号恢复、解析证据交叉验证、澄清选项生成。
- `tests/test_metric_ambiguity_confirmation_flow.py`：10 项门禁与同轮锁定测试。

### 修改文件

- `state/base_state.py`：`TopicState` 增加 `analysis_spec`，跨轮保留候选与解析证据。
- `state/query_plan.py`：`QueryPlan` 增加可选 `concept_resolutions` 审计字段。
- `services/query_plan_service.py`：`lock_query_plan` 接收并写入 `concept_resolutions`。
- `tools/advisor_tools.py`：提取 `search_column_candidates()` 结构化候选。
- `tools/submit_query_plan.py`：增加 `concept_resolutions` 审计参数。
- `graphs/advisor_graph.py`：集成门禁；拦截时不生成 locked_plan 并程序生成候选选项；指标未解析前历史案例只注入问题摘要；解析结果回写 `analysis_spec`。
- `prompts/planner_prompt.py`：`PlannerOutput` 增加 `metric_mentions/dimension_mentions/analysis_type`，只提取业务概念不选物理字段。
- `nodes/planner_node.py`：组装初始 AnalysisSpec 并保留已有解析记录。
- `prompts/advisor_prompt.py`：补充指标口径解析与同轮锁定规则。
- `runtime/graph_logger.py`：新增 `log_metric_event` 结构化事件。

## 五、关键设计

1. **候选必须来自真实元数据**：门禁直接使用 `column_vector_store` 检索结果和 Planner/Advisor 结构化候选，不允许 LLM 编造字段。
2. **只对 measure 判歧义**：维度候选单独处理，避免维度字段干扰指标判断。
3. **强匹配优先**：用户输入词出现在候选注释/字段名中为强匹配；别名包含为弱匹配（如“B类新增订单”包含别名“新增订单”），只有强匹配唯一时才构成 `explicit_user`。
4. **编号恢复语义**：用户回复“1/2/B”时，结合上一轮 `metric_resolutions.candidates` 顺序恢复完整语义。
5. **解析证据交叉验证**：LLM 提交的 `concept_resolutions` 必须与程序重算一致，`ambiguous` 指标即使 LLM 声称已解决也会被拒绝。
6. **保持同轮锁定**：用户本轮解决歧义后，Advisor 当轮直接调用 `submit_query_plan` 生成 locked 方案，不增加确认轮。

## 六、验证场景

- “新增订单”存在多个候选且用户未选择 → 门禁拦截，返回候选选项，不生成 locked_plan。
- 高相似历史案例使用 `new_order` → 不能绕过门禁。
- 用户回答“全量新增订单” → 本轮锁定 `new_order`。
- 用户回答“净增订单” → 本轮锁定 `really_add_order`。
- 用户回答“B类新增订单” → 本轮锁定 `new_b_order`。
- 用户回复选项编号“2” → 结合上一轮候选恢复为 `really_add_order`。
- 唯一指标候选 → 直接 `unique_metadata` 放行，无额外确认轮。
- LLM 伪造 `resolution_source=explicit_user` → 程序拒绝。
- 多表复合 Join、逐表 `pt_dt`、LEFT JOIN 过滤语义与业务过滤隔离回归通过。

## 七、当前边界

- `semantic_default` 依赖正式语义层配置，第13-14课接入后启用。
- 指标概念与候选的关联目前依赖向量语义召回 + 注释/别名匹配，尚未使用完整指标口径文档。
- 维度概念（`dimension_mentions`）当前只做结构化保存，维度歧义门禁待后续课程完善。
- 门禁运行在 Advisor 子图内，Planner 侧暂不重复执行，日志可还原完整判定过程。

## 八、面试表达

> “我把‘用户确认口径’从 Prompt 约束升级为程序门禁。Planner 只提取指标业务概念，不替用户选物理字段；Advisor 提交方案时，程序基于真实元数据候选和用户本轮选择重算解析证据，只有 explicit_user、unique_metadata 或正式语义默认三种来源可信。历史案例和最高相似度只能参与排序，不能证明口径。多候选未选择时程序拦截锁定并生成候选选项，用户回答‘净增订单’或选项编号后同轮锁定，不增加重复确认。这样既解决了模型被历史案例锚定的问题，又保留了唯一候选和同轮锁定能力。”

## 九、迭代优化（2026-08-05 确认消息收敛与描述简化）

### 确认消息不带候选

- 门禁通过后，`proposed_plan["measures"]` 用 `concept_resolutions` 收敛，只保留用户已确认口径的指标字段，候选不进入 `locked_plan`。
- `_build_confirmation_message` 展示层兜底过滤，确认消息只出现最终锁定的表、指标、维度、时间，不出现编号选项或候选列表。

### 描述只用原始备注

- 字段中文含义改为优先“原始备注”，避免把整串别名展示出来（如 `new_order` 别名有 5 个，原备注只有“新增订单数”）。
- 表描述改用表自带备注 `original_comment`，不使用增强后的长备注。
- 澄清选项只展示“一句中文含义（字段名）”，多表候选追加来源表短名，全部由程序模板生成，不展示 LLM 原文。

### 配置化候选收敛

- `config/advisor.py` 新增 `MAX_AMBIGUITY_CANDIDATES`、`MIN_CANDIDATE_SCORE`、`EXAMPLE_FIELD_BOOST`，候选展示上限、相似度下限和优秀案例排序加权均可调。

### 验证

- 确认消息示例：`指标：新增订单数（new_order）`、`维度：经销商名称、状态、业务经理、联系方式`，不出现其他候选字段。
- 回归测试：指标歧义门禁与多表确认协议共 20 项全部通过；`py_compile` 通过。
