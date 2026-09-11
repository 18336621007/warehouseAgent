# skill 机制（通用 agent 能力）

> 目标：给系统增加 Codex 风格的通用 skill 能力——场景化指令包由用户编写，系统自动匹配并注入主协调者（Planner）上下文，指导 agent 行为。
> 定位：skill 是「决策策略层」，与语义层 / RAG / 落盘结果 / 工具完全正交，不替代任何一层，只决定「这个场景怎么组合底层能力」。

## 一、目录结构

```
agentTest/skills/                  # skill 数据，用户可增删改
  <skill-name>/
    SKILL.md                     # 必选：frontmatter + 指令正文
    references/                  # 可选：skill 自带资源（faq.yaml / 领域知识 / 样例）
agentTest/langgraph_app/skills/   # skill 代码（与数据分离）
  skill_loader.py                # 通用加载/匹配/格式化
```

## 二、SKILL.md 格式

```yaml
---
name: <skill-name>                      # 唯一名
description: 一句话描述触发场景（用于匹配）
version: 1.0.0
trigger_keywords: [关键词1, 关键词2]      # 可选，匹配用
scope: [planner]                        # 注入范围：planner 或 [planner, advisor]
---
## 触发条件
## 场景行为指令      ← Markdown，LLM 读取执行；程序不解析内容
## 可用资源
## 拒绝与兜底
```

**原则**：程序只解析 frontmatter（name/description/trigger_keywords/scope），正文业务规则全部由 LLM 执行，用户可随意编辑。

## 三、加载 / 匹配 / 注入

1. **加载**：`build_skill_manager()` 扫描 `agentTest/skills/*/SKILL.md`，解析 frontmatter 为 `SkillSpec`（runtime 启动时注册，`runtime["skill_manager"]`）。
2. **匹配**：`match_skills(question, scope)`——trigger_keywords 在问题中命中，或 description 分词/整体命中；命中数降序，零 LLM、可审计。
3. **注入**：`planner_node` 组装 ReAct 消息时，在 `PLANNER_SYSTEM_PROMPT` 之后追加命中 skill 的 `SystemMessage`；不进前端思考过程/工具流水单。skill 增删无需改代码。

## 四、与现有能力的关系

| 层 | 职责 | 谁拥有 |
|---|---|---|
| skill（决策策略层） | 什么提问走什么路、口径规则、展示规则、FAQ 直达 | 用户写，LLM 执行 |
| 语义层 | 指标→表/表达式/维度的权威口径 | agentTest/semantic_layer/ |
| RAG | 语义层未命中时的表/字段检索兜底 | 已有向量/BM25 |
| 落盘结果 | 追问复用，不重查库 | result_store |

## 五、如何新增一个 skill

1. 复制 `agentTest/skills/example/` 为 `agentTest/skills/<你的名字>/`
2. 编辑 SKILL.md：改 frontmatter 的 name/description/trigger_keywords/scope，写正文指令
3. 可选：建 `references/` 放 faq.yaml 等资源（注入时会给出资源目录路径，LLM 按需读取）
4. 重启服务生效；日志 `skill.matched` 可确认命中情况

## 六、路由稳定改动（S0，本批次）

为解决「Planner 漏调 search_semantic → 语义路径被禁 → 降级 Advisor → 死循环」，本次做了：

- **S0-A 去硬门禁**：删除 `called_semantic_tool` 门禁，语义路径只看 `semantic_metrics` 非空且置信度达标；指标 id 真实性由 provider 反查校验。LLM 判 `answer` 可直接回答，判 `seeker` 且能给表则直接执行。
- **S0-B 修 minimal 死区**：fields 空但有 tables/filters 时，从 filters 推导 time_field/time_range，且不再因「measures/dimensions 同时为空」校验失败——聚合表达式交给 Seeker 的 generate_sql 按 effective_query 生成。
- **S0-C 保留保险丝**：`MAX_ADVISOR_AUTO_CONTINUE=3` 防 planner↔advisor 死循环。

调不调语义层完全由 skill 指令 + LLM 判断，程序只负责 id 真实性校验、方案结构校验与安全，不主动补调用。

## 七、日志怎么看

- `skill.matched`：本次命中哪些 skill（`name`、`hit_count`）
- `semantic.match`：语义层命中情况（`tier`：unique/candidate/rag）
- `tools.called`：实际调用工具（是否调了 `search_semantic` / `query_stored_result`）
- 通过三类事件组合可审计「这次走了 skill / 语义层 / RAG / 落盘结果」
