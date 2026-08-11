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

**最终方案**：新增 `AnalysisSpec`、`MetricAmbiguityValidator` 与 `MetricClarificationService`。候选合法性由 Validator 校验；上一轮候选编号、字段名和中文含义由澄清服务基于 State 确定性解析；Advisor 的 `llm_submitted` 只作为补充输入，不能覆盖用户已经明确选择的字段。历史案例和最高相似度只能参与候选排序，不能产生解析证据。

## 四、实现内容

### 新增文件

- `state/analysis_spec.py`：`AnalysisSpec` + `ConceptResolution` TypedDict。
- `services/metric_ambiguity_validator.py`：候选收集、字段合法性校验、已解析证据保持与澄清选项生成。
- `services/metric_clarification_service.py`：固化候选顺序、解析编号/字段名/中文含义、维护 pending 状态并生成澄清文本。
- `tests/test_metric_ambiguity_confirmation_flow.py`：覆盖首轮回写、编号选择、候选重排和状态清理。

### 修改文件

- `state/base_state.py`：`TopicState` 增加 `analysis_spec`，跨轮保留候选与解析证据。
- `state/query_plan.py`：`QueryPlan` 增加可选 `concept_resolutions` 审计字段。
- `services/query_plan_service.py`：`lock_query_plan` 接收并写入 `concept_resolutions`。
- `tools/advisor_tools.py`：提取 `search_column_candidates()` 结构化候选。
- `tools/submit_query_plan.py`：增加 `concept_resolutions` 审计参数。
- `graphs/advisor_graph.py`：保留 Agent 编排和门禁调用；首轮候选通过澄清服务回写 `analysis_spec`；已确认字段注入 Advisor 上下文并由程序收敛 `measures`；日志记录最终用户可见回复。
- `prompts/planner_prompt.py`：`PlannerOutput` 增加 `metric_mentions/dimension_mentions/analysis_type`，只提取业务概念不选物理字段。
- `nodes/planner_node.py`：在调用后续门禁前优先消费 pending 选择，并基于已有 AnalysisSpec 增量更新，避免短回答重置解析状态。
- `prompts/advisor_prompt.py`：补充指标口径解析与同轮锁定规则。
- `runtime/graph_logger.py`：新增 `log_metric_event` 结构化事件。

## 五、关键设计

1. **候选必须来自真实元数据**：门禁直接使用 `column_vector_store` 检索结果和 Planner/Advisor 结构化候选，不允许 LLM 编造字段。
2. **只对 measure 判歧义**：维度候选单独处理，避免维度字段干扰指标判断。
3. **固定候选快照**：首次发现歧义时把 `clarification_id / mention / options` 写入 `pending_metric_clarification`，编号含义不随后续召回排序变化。
4. **确定性选择优先**：Planner 在 LLM 之前解析 `2`、`第二个`、精确字段名和完整中文含义，只使用上一轮真实 options，不写死业务指标。
5. **已确认字段保持有效**：重新召回缺少该字段或候选顺序变化时，只要字段存在于上轮真实候选，用户选择仍然有效。
6. **程序状态高于 LLM 漏传**：Advisor 即使未提交 `concept_resolutions`，Validator 也会沿用 State 中的 resolved 记录；如果 LLM 的 `measures` 不一致，程序直接按用户已确认字段收敛。
7. **保持同轮锁定**：用户本轮解决歧义后，Advisor 当轮直接调用 `submit_query_plan` 生成 locked 方案，不增加确认轮。

## 六、验证场景

- “新增订单”存在多个候选且用户未选择 → 门禁拦截，返回候选选项，不生成 locked_plan。
- 高相似历史案例使用 `new_order` → 不能绕过门禁。
- 用户回答“全量新增订单” → 本轮锁定 `new_order`。
- 用户回答“净增订单” → 本轮锁定 `really_add_order`。
- 用户回答“B类新增订单” → 本轮锁定 `new_b_order`。
- 用户回复“2”“第二个”“really_add_order”或“净增订单数” → 结合固定 pending options 恢复为 `really_add_order`。
- 唯一指标候选 → 直接 `unique_metadata` 放行，无额外确认轮。
- LLM 伪造 `resolution_source=explicit_user` → 程序拒绝。
- 多表复合 Join、逐表 `pt_dt`、LEFT JOIN 过滤语义与业务过滤隔离回归通过。

## 七、当前边界

- `semantic_default` 依赖正式语义层配置，第14-15课接入后启用。
- 指标概念与候选的关联目前依赖向量语义召回 + 注释/别名匹配，尚未使用完整指标口径文档。
- 维度概念（`dimension_mentions`）当前只做结构化保存，维度歧义门禁待后续课程完善。
- 门禁运行在 Advisor 子图内，Planner 侧暂不重复执行，日志可还原完整判定过程。

## 八、面试表达

> “我把用户确认口径从 Prompt 约束升级为程序状态机。首次发现多候选时，系统把候选顺序固化到 pending clarification；下一轮 Planner 在 LLM 前确定性解析编号、字段名或中文含义。Validator 只校验真实字段和解析证据，Advisor 漏传或改写 measures 时由程序按用户选择收敛。这样既避免历史案例锚定，也避免模型已经理解第二个但 State 仍停留在 ambiguous 导致的循环确认。”

## 九、迭代优化（确认消息收敛与描述简化）

### 确认消息不带候选

- 门禁通过后，`proposed_plan["measures"]` 用 `concept_resolutions` 收敛，只保留用户已确认口径的指标字段，候选不进入 `locked_plan`。
- `_build_confirmation_message` 展示层兜底过滤，确认消息只出现最终锁定的表、指标、维度、时间，不出现编号选项或候选列表。

### 描述只用原始备注

- 字段中文含义改为优先“原始备注”，避免把整串别名展示出来（如 `new_order` 别名有 5 个，原备注只有“新增订单数”）。
- 表描述改用表自带备注 `original_comment`，不使用增强后的长备注。
- 澄清选项只展示“一句中文含义（字段名）”，多表候选追加来源表短名，全部由程序模板生成，不展示 LLM 原文。
- 元数据来源分层：`enriched_columns` 新增 `meta_source`（`ddl_comment`/`llm_enhanced`）；澄清选项、口径区别说明、方案确认统一优先展示字段原始备注，无原始备注才回退增强别名并标注“系统推断别名”，避免增强元数据误导用户。

### 配置化候选收敛

- `config/advisor.py` 新增 `MAX_AMBIGUITY_CANDIDATES`、`MIN_CANDIDATE_SCORE`、`EXAMPLE_FIELD_BOOST`，候选展示上限、相似度下限和优秀案例排序加权均可调。

### 验证

- 确认消息示例：`指标：新增订单数（new_order）`、`维度：经销商名称、状态、业务经理、联系方式`，不出现其他候选字段。
- 回归测试：指标歧义门禁与多表确认协议共 29 项全部通过；`py_compile` 通过。

## 十、迭代优化（跨轮状态闭环）

### 循环确认的真实原因

日志显示用户回复“第二个”后，Planner 已把有效需求还原为目标字段，但 `analysis_spec.metric_resolutions` 仍保留上轮 `ambiguous`；Advisor 又依赖 LLM 可选参数 `concept_resolutions`，因此门禁反复认为口径未解决。另一个问题是首轮无 `submit_query_plan` 时只生成了候选文本，没有把候选写回 State，下一轮编号缺少稳定事实来源。

### 本次修复

- `AnalysisSpec` 增加 `pending_metric_clarification`。
- 新增 `MetricClarificationService`，从 `advisor_graph.py` 拆出候选固化、选择解析、状态回写和澄清文本构造。
- Planner 在组装新 AnalysisSpec 前优先解析 pending，命中后写入 `resolution_source=explicit_user` 并清理 pending。
- Advisor 对提交校验和无提交预校验统一回写结果，保证首轮候选必定进入 State。
- 已确认字段注入 Advisor 上下文；LLM 漏传或提交其他 measure 时，程序使用用户确认字段覆盖。
- `MetricAmbiguityValidator` 认可上轮真实候选，避免重新检索排序变化使 resolved 失效。
- `node.end.answer_summary` 改为记录最终用户可见回复，不再记录被程序覆盖的 LLM 草稿。

### 当前边界与下一课

当前维护单个活动指标 pending，支持紧接着回复编号，也支持先询问口径差异、隔数轮再回复编号（延迟澄清恢复）。查询成功后由 `last_query_result` 保存结果快照，支持基于结果的追问。多 pending 冲突消解等能力经评估不引入；若未来出现多个未解决 pending，系统必须要求用户指定概念，不能猜测“第二个”属于哪一组候选。
