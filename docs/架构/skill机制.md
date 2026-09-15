# skill 机制（渐进式披露，仿 Codex）

> 最后更新：2026-09-15
> 目标：给系统增加 Codex 风格的通用 skill 能力——场景化指令包由用户编写，系统采用**渐进式披露**：只向模型披露技能的 name+description，由模型自主判断是否读取完整指令。
> 定位：skill 是「决策策略层」，与语义层 / RAG / 落盘结果 / 工具完全正交，不替代任何一层，只决定「这个场景怎么组合底层能力」。

## 一、目录结构

```text
agentTest/skills/                  # skill 数据（用户可增删改）
  <skill-name>/
    SKILL.md                     # 必选：frontmatter + 指令正文
    references/                  # 可选：skill 自带资源（faq.yaml / 领域知识 / 样例）
agentTest/langgraph_app/skills/
  skill_loader.py                # 加载 / 披露索引 / 按需读取
agentTest/langgraph_app/tools/
  skill_tool.py                  # read_skill 工具（LLM 按需读取完整正文）
```

## 二、SKILL.md 格式

```yaml
---
name: <skill-name>                      # 唯一名
description: 一句话描述触发场景（LLM 据此判断是否读取）
version: 1.0.0
scope: [planner]                        # 注入范围：planner（当前仅 planner 读取）
---
## 触发条件
## 场景行为指令      ← Markdown，LLM 读取执行；程序不解析正文
## 可用资源
## 拒绝与兜底
```

**原则**：
- 程序只解析 frontmatter（name / description / scope），正文业务规则全部由 LLM 执行，用户可随意编辑。
- `trigger_keywords` 已**不再参与程序匹配**（保留在 frontmatter 仅为兼容，仅 `match_skills` 兼容方法使用）；匹配完全由 LLM 根据 `description` 判断。
- `description` 很关键：模型靠它判断当前任务是否触发该 skill，建议一句话写清「何时该用 / 何时不该用」。

## 三、渐进式披露流程

```text
启动时：build_skill_manager() 扫描 agentTest/skills/*/SKILL.md
        └─ 只解析 frontmatter：name / description / scope（正文不预加载）

每轮 Planner：
① 注入【可用技能】索引（SystemMessage，追加在 PLANNER_SYSTEM_PROMPT 之后）
   └─ list_skills_index(scope="planner", max_chars=SKILL_INDEX_MAX_CHARS)
   └─ 只列出 name + description，预算截断（默认 4000 字符，超了先截短 description）
② 模型看到索引，自主判断
   └─ 当前任务匹配某技能 description → 调用 read_skill(技能名)
   └─ 不匹配 → 不读取，不占上下文
③ read_skill 工具返回该技能完整 SKILL.md 正文 + references 路径
   └─ 正文作为工具结果回填（read_skill 结果放宽到 12000 字符，完整进上下文）
   └─ 模型遵循正文规则（FAQ 快速路径、口径追问、回答模板等）
④ references 资源（如 faq.yaml）仍按需读取：正文只给路径，LLM 命中相关章节时再读
```

关键点：
- **程序只做两件事**：披露索引（每轮）+ 按名读正文（read_skill 工具）；**匹配决策完全交给模型**。
- **上下文成本可控**：平时只占几行索引（≤4000 字符），只有模型选中某技能才加载完整正文。
- **多技能可并存**：索引列出全部，模型可依次 `read_skill` 读取多个相关技能。
- **scope 过滤**：索引与读取都限制在 `planner` 作用域（如 gy-offline-nlq 的 scope 为 `[planner]`）。

## 四、read_skill 工具

- 注册在 Planner 工具组（`groups=("planner",)`），`ToolSecurity` 只读。
- 入参 `skill_name`；返回该技能完整指令正文 + 技能资源目录路径。
- 找不到技能 / scope 不符时返回明确提示，不抛错。
- Planner 系统提示词中已声明：任务匹配【可用技能】列表某技能 description 时调用，不匹配则不调用。

## 五、预算与配置

| 配置项 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| 技能索引披露预算（字符） | `SKILL_INDEX_MAX_CHARS` | 4000 | 对齐 Codex：初始列表只占少量上下文 |

## 六、与现有能力的关系

| 层 | 职责 | 谁拥有 |
|---|---|---|
| skill（决策策略层） | 什么提问走什么路、口径规则、展示规则、FAQ 直达 | 用户写，LLM 执行 |
| 语义层 | 指标→表/表达式/维度的权威口径 | agentTest/semantic_layer/ |
| RAG | 语义层未命中时的表/字段检索兜底 | 已有向量/BM25 |
| 落盘结果 | 追问复用，不重查库 | result_store |

## 七、如何新增一个 skill

1. 复制 `agentTest/skills/example/` 为 `agentTest/skills/<你的名字>/`。
2. 编辑 SKILL.md：改 frontmatter 的 name / description / scope，写正文指令。`description` 写清触发场景（对齐 Codex 建议：把关键触发场景前置，描述被截短时仍能识别）。
3. 可选：建 `references/` 放 faq.yaml 等资源（read_skill 返回正文时会给出资源目录路径，LLM 按需读取）。
4. 重启服务生效；通过日志确认模型是否自主读取：`skill.matched`（披露）+ `tools.called`（是否调 read_skill）。

## 八、日志怎么看

- `skill.matched`：本次披露了哪些技能（`name` 列出全部、`hit_count`、`mode=progressive_disclosure`）。
- `tools.called`：实际调用的工具，确认模型是否自主调了 `read_skill`（再配合 `search_semantic` / `query_stored_result` 判断整体走向）。
- `semantic.match` / `search.scores`：语义层命中与 RAG 召回情况。
- 组合以上事件可审计「这次走了 skill / 语义层 / RAG / 落盘结果」。

## 九、演进记录

- **改造前**：`match_skills` 用 trigger_keywords + description 做**程序关键词匹配**，命中就把 skill 正文全量注入 prompt（零 LLM、确定性强，但泛化差、正文常驻上下文）。
- **改造后（2026-09-15）**：仿 Codex 渐进式披露——只披露 name+description 索引，由 LLM 判断是否 `read_skill` 读取完整正文。`match_skills` / `format_instruction` 保留用于兼容与单测，Planner 主流程不再调用。
