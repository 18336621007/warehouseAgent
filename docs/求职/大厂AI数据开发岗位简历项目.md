# 大厂 AI 数据开发岗位简历项目

> 最后更新：2026-08-04
> 目标岗位：AI 数据开发工程师、数据平台研发工程师、智能数据产品研发、Data+AI 工程师
> [返回文档索引](../文档索引.md)

## 一、岗位对标结论

当前项目最适合定位为“面向企业数仓的多智能体 NL2SQL / Data Agent 平台”，而不是单纯的 Prompt Demo。项目能够证明以下能力：

- 将 LLM、RAG、元数据、数据仓库和安全执行链组合为可运行系统。
- 用结构化 QueryPlan、State 和 Guardrails 控制模型，而不是完全依赖自然语言 Prompt。
- 处理多轮澄清、多表 Join、字段来源、分区过滤、SQL 校验和真实 Hive 执行。
- 通过结构化日志定位线上式问题，并形成自动修复、重试、降级和评估闭环。
- 能够说明数据工程、AI 工程和平台工程之间的边界与取舍。

与互联网大厂岗位要求相比，项目已经覆盖 NLQ、RAG、Data Agent、元数据、查询安全、可观测性和数据产品化思路；仍需继续补充 Spark/Flink/Ray 等分布式计算、大规模离线评测集、服务持久化、并发治理和量化性能指标。

## 二、简历项目名称

推荐名称：

**智能数仓助手：基于 LangGraph 的多智能体 Text2SQL 与安全多表查询平台**

备选名称：

- 企业级 Data Agent：自然语言到 Hive SQL 的多智能体问数系统
- 基于 RAG、QueryPlan 与 Guardrails 的智能数仓问数平台
- 面向 Hive 数仓的多 Agent NL2SQL 规划、执行与评估系统

## 三、一句话项目描述

基于 LangGraph、LangChain、FAISS、MySQL 元数据和 Hive 构建多智能体智能问数平台，通过 Planner、Advisor、Seeker、Evaluator 协作，将业务自然语言转换为可审计 QueryPlan 和安全 Hive SQL，支持状态化指标口径澄清、多表复合 Join、逐表分区过滤、自动修复、执行结果解释与质量评估。

## 四、简历可直接使用的项目描述

### 4.1 推荐版：5 条核心项目经历

**智能数仓助手｜多智能体 Text2SQL 与安全多表查询平台**
技术栈：Python、LangGraph、LangChain、OpenAI Compatible API、FAISS、MySQL、Hive、Flask、SQL AST、JSON Lines

- 设计 Planner、Advisor、Seeker、Evaluator 多智能体协作架构，以 LangGraph StateGraph 编排“需求识别—业务澄清—方案锁定—SQL 生成—安全执行—质量评估”链路，将非确定性 LLM 输出收敛为可校验的 QueryPlan。
- 建设库、表、字段三级元数据 RAG 和 confirmed-plan 驱动的精确 Schema 加载机制，使用 FAISS 进行候选召回，并通过字段来源锁定避免执行阶段静默换表，降低元数据误召回对 SQL 正确性的影响。
- 实现多表字段覆盖分析与 JoinPlanner，支持人工关系元数据、复合 Join 键及 AI Join 推测开关；在关系缺失、Join 键不完整或表不可达时执行安全拒绝或受控推测。
- 构建 Hive SQL 多层 Guardrails：只读与白名单校验、LIMIT 和分区保护、笛卡尔积检测、Join 键校验、全局逐表必选过滤、LEFT JOIN 过滤语义保护；缺少维表分区条件时根据 confirmed QueryPlan 确定性重建 SQL。
- 建设 request/conversation/topic/thread 四级身份模型和 JSONL 全链路日志，记录节点耗时、路由决策、State 变化、SQL 重试与执行结果；通过日志定位“模型已理解编号但 State 仍 ambiguous”的循环确认问题，设计 pending clarification 固化候选并在 Planner 前确定性解析。

### 4.2 偏 AI 工程版本

- 使用 LangGraph 构建 Planner—Advisor—Seeker 多 Agent 工作流，将 LLM 用于意图理解、业务歧义消解和 SQL 生成，将方案确认、字段覆盖、Join 规划和安全校验下沉为确定性代码。
- 设计 Adaptive Advisor 与指标澄清服务：首次多候选写入 pending，支持数字、中文序号、字段名和中文含义选择；用户选择后由程序收敛 measures，同轮生成 locked QueryPlan。
- 结合相似高质量问数示例进行 Few-shot 检索，并由 Evaluator 对答案完整度、用户体验、耗时、轮次和自评质量进行打分，沉淀可复用样本。
- 通过 Prompt 约束、工具协议、Pydantic/TypedDict 契约、图级拦截、SQL Guardrails 和结构化日志形成多层 Agent 安全体系。

### 4.3 偏数据开发版本

- 采集 Hive Schema 并通过 LLM 增强字段业务语义，写入 MySQL 元数据存储，增量构建库/表/字段 FAISS 索引，为自然语言问数提供语义检索能力。
- 针对事实表、快照维表和汇总表设计表粒度、主键、时间字段、Join 基数及版本化关系元数据，支持 `pt_platform + company_id` 等复合关联。
- 强制所有参与表按照 Guardrails 配置分别添加 `pt_dt` 等过滤字段，避免 Hive 快照表历史分区重复 Join 导致指标膨胀，并将右表过滤放入 JOIN ON 保持 LEFT JOIN 语义。
- 对生成 SQL 执行只读、白名单、分区、LIMIT、Join、字段来源和方案一致性校验，失败时进入自动修复、LLM 重试或降级 SQL 构造。

## 五、项目亮点的 STAR 表达

### 5.1 重复确认问题

- **Situation**：用户已经确认 Advisor 展示的完整方案，但系统仍要求再次确认才能执行。
- **Task**：在不降低 QueryPlan 安全性的前提下，将最终确认压缩为一次。
- **Action**：分析 JSONL 日志发现 Advisor 在 `locked=false` 时输出“已确认方案”，而 Planner 只有检测到真实 locked 方案才允许进入 Seeker；将 Advisor 改为 Adaptive 模式，在本轮元数据核验完成后直接调用 `submit_query_plan`，并对未锁定回复统一增加“仅用于继续核对”标识。
- **Result**：目标流程收敛为“必要澄清—同轮锁定—用户最终确认一次—执行”，消除伪确认状态。

### 5.3 指标编号循环确认

- **Situation**：Advisor 已给出多个指标选项，用户回复“第二个”，Planner 也还原出目标字段，但系统仍不断要求确认。
- **Task**：在不写死具体业务指标、不依赖 LLM 必须正确填写可选参数的前提下，建立跨轮可审计的选择状态。
- **Action**：通过 `topic_id` 日志发现首轮候选未写回 State、Planner 重建 AnalysisSpec 时保留旧 ambiguous、Advisor 门禁过度依赖 `concept_resolutions`；新增 `MetricClarificationService`，固化 `clarification_id + options`，在 Planner 调用 LLM 前解析编号/字段名/中文含义，并让程序使用 resolved 字段覆盖 LLM measures。
- **Result**：候选重排和 LLM 漏传不再导致 resolved 回退，指标确认与多表确认 29 项测试通过；架构为后续延迟选择和连续问答提供 pending 状态基础。
### 5.2 多表历史分区膨胀

- **Situation**：多表 SQL 的 Join 键正确，但维表没有 `pt_dt` 条件，历史快照重复参与 Join，指标被放大。
- **Task**：保证所有参与表都执行独立分区裁剪，并且不破坏 LEFT JOIN 语义。
- **Action**：在 Guardrails 中增加全局必选过滤字段配置；解析 SQL 表别名和 ON/WHERE 谓词，区分真实常量过滤与字段对字段 Join；简单查询缺失过滤时根据 QueryPlan 重建 SQL，将维表条件放入对应 JOIN ON。
- **Result**：形成“配置—生成—自动修复—执行前门禁”四层保障，逐表分区过滤覆盖成为可测试规则。

### 5.3 LLM SQL 不稳定

- **Situation**：模型会遗漏聚合函数、Join 条件或表过滤，同一 Prompt 多次生成结果不一致。
- **Task**：降低对模型随机性的依赖，保证执行 SQL 可解释、可追溯。
- **Action**：构建 `_validate_sql_against_plan` 程序校验和 LLM SQL Audit 双层审计；支持带别名聚合识别；重试耗尽后使用 confirmed QueryPlan 构造标准 SQL。
- **Result**：LLM 负责表达与复杂推理，确定性代码负责安全边界和可恢复路径。

## 六、两分钟项目介绍

我做的是一个面向 Hive 数仓的多智能体智能问数系统。用户只需要描述业务问题，系统会经过 Planner、Advisor 和 Seeker 三个主要 Agent。Planner 负责还原当前完整需求和路由；Advisor 会调用元数据工具确认指标、维度和表，并生成 locked QueryPlan；用户最终确认一次后，Seeker 根据 QueryPlan 完成字段覆盖、Join 规划、Schema 加载、SQL 生成、校验和 Hive 执行。

这个项目重点不只是生成 SQL，而是控制 LLM 的不确定性。我设计了 QueryPlan 契约、字段来源、逐表 table_plan、复合 Join 键、AI Join 推测开关和多层 Guardrails。比如所有参与表必须根据配置独立过滤 `pt_dt`，事实表条件放 WHERE，LEFT JOIN 维表条件放 ON；如果模型遗漏维表分区，系统会优先根据已确认方案自动重建安全 SQL。

我还建立了 conversation、topic、request、graph thread 四级身份和 JSONL 全链路日志。实际通过日志定位过两个问题：一个是 Advisor 没有真正锁定方案却让用户确认，造成重复确认；另一个是维表缺少日期分区造成数据膨胀。项目目前已完成多表问数、AnalysisSpec 和指标确认闭环。下一阶段先建设连续问答：保存结构化结果快照，让用户基于上一轮结果追问，并把单 pending 升级为可跨数轮恢复的澄清注册表；随后进入最小语义层和 BM25 + RAG 混合检索。

## 七、30 秒项目介绍

我基于 LangGraph 做了一个面向 Hive 数仓的多智能体 Text2SQL 系统。它不是让大模型直接猜 SQL，而是通过 Planner、Advisor、Seeker 把需求澄清、QueryPlan、字段来源、Join 路径和 SQL 执行拆开，并使用 FAISS 元数据检索、复合 Join、逐表分区过滤、SQL Guardrails、自动修复和全链路日志保证安全性。项目已经支持真实 Hive 多表查询，能够通过日志定位重复确认和快照维表数据膨胀问题。

## 八、能力对标矩阵

| 大厂岗位常见能力 | 项目证据 | 当前程度 | 后续补强 |
|---|---|---|---|
| Python 工程能力 | LangGraph 节点、服务层、数据源、校验器、测试 | 已覆盖 | 增加类型检查、CI、包管理 |
| SQL 与数仓 | Hive SQL、事实/维表、分区、粒度、Join 基数 | 已覆盖 | 增加窗口分析和成本优化 |
| NLQ / Text2SQL | QueryPlan、Schema RAG、Few-shot、SQL 生成 | 已覆盖 | 建立标准离线评测集 |
| RAG 与元数据 | FAISS 三级索引、MySQL 增强元数据 | 已覆盖 | 增加混合检索、重排和版本治理 |
| Agent 工程 | 多 Agent、工具调用、State、路由、重试 | 已覆盖 | 增加并发、幂等、人工审批 |
| 数据安全 | 白名单、只读、分区、LIMIT、Join 校验 | 已覆盖 | 增加数据权限、脱敏和审计平台 |
| 可观测性 | JSONL、ContextVar、节点耗时、State 变化 | 已覆盖 | 接入 OpenTelemetry 和指标平台 |
| 分布式数据处理 | 当前主要使用 Hive 查询 | 待补 | Spark/Flink 离线与实时任务 |
| AI 数据流水线 | 元数据增强、示例沉淀、Evaluator | 部分覆盖 | 数据版本、评测集、标注和质量平台 |
| 服务化与高可用 | Flask Web、错误边界 | 部分覆盖 | 持久化 Checkpoint、并发、容器部署 |

## 九、量化指标写法

简历中不要编造业务数据。当前可以真实写：

- 构建 Planner、Advisor、Seeker、Evaluator 4 类 Agent/角色。
- 支持库、表、字段 3 层向量索引。
- 多表安全链覆盖 QueryPlan、Coverage、Join、Schema、Generate、Validate、Execute 7 个阶段。
- 新增 10 项多表与确认协议回归测试，原有 SQL 安全样例 7/7 通过。
- 真实日志能够还原节点耗时；当前案例中 Hive 执行约占请求总耗时的主要部分。

上线或扩大测试后再补：

- SQL 首次成功率。
- 自动修复成功率。
- 平均澄清轮次。
- 多表逐表分区覆盖率。
- 查询 P50/P95 延迟。
- 人工验收正确率和高质量样本沉淀率。

## 十、简历关键词

`LangGraph`、`LangChain`、`Data Agent`、`Text2SQL`、`NL2SQL`、`RAG`、`FAISS`、`Hive`、`MySQL`、`QueryPlan`、`Metadata`、`SQL Guardrails`、`Multi-Agent`、`Structured Logging`、`Evaluator`、`Schema Linking`、`Join Planning`、`Prompt Engineering`、`Data Quality`

## 十一、诚实描述当前不足

面试中应主动说明：

- 当前 Checkpointer 是 MemorySaver，不支持服务重启恢复。
- AI Join 推测属于可配置风险能力，生产环境通常建议默认关闭或增加人工审核。
- 当前关系图规模较小，尚未验证上千张表的路径搜索和元数据版本治理。
- 自动 SQL 构造主要覆盖普通聚合和“昨天”等标准时间场景，复杂窗口分析仍依赖 LLM 修复。
- 尚未建立 Spider/BIRD 风格或企业真实问数集的系统化离线评测。

主动说明边界不会减分，关键是同时给出下一阶段设计。

## 十二、岗位能力参考

本项目能力对标参考了以下官方岗位描述：

- [字节跳动 Data-Foundation-LLM / AI 数据基础设施岗位](https://jobs.bytedance.com/en/position/7532079198892779783/detail)
- [腾讯 Data+AI / 智能数据产品方向岗位](https://careers.tencent.com/en-us/jobdesc.html?postId=1889049186362462208)

岗位共同强调数据与模型协同、数据基础设施、NLQ/RAG/Agent、数据质量、DataOps/MLOps 和平台化能力。简历应突出已经完成的工程闭环，同时用后续规划补充 Spark、Flink、Ray、评测平台和生产部署能力。