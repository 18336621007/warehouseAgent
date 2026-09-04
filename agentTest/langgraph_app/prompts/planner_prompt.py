# Planner LLM 结构化输出的 Pydantic 模型 + Prompt 模板
from typing import Literal

from pydantic import BaseModel, Field

class SemanticMetricHit(BaseModel):
    """Planner 对语义层候选指标的置信度判定（第3层）。"""

    id: str = Field(
        default="",
        description="语义层命中指标的 id"
    )

    confidence: float = Field(
        default=0.0,
        description="置信度（0~1）：>=0.9 名称/别名强命中唯一口径；0.55~0.9 定义/备注弱命中需澄清；<0.55 视为无关"
    )

    mention: str = Field(
        default="",
        description="用户问题中命中该指标的关键词"
    )


class SemanticKeywordsOutput(BaseModel):
    """第0层：从用户问题中拆出用于语义层全文检索的业务关键词。"""

    semantic_keywords: list[str] = Field(
        default_factory=list,
        description="业务检索词，剔除时间词/查询动词/实体词，每个词尽量短且独立可检索"
    )


class PlannerOutput(BaseModel):
    """Planner 对当前有效需求的模糊度分析结果。"""

    effective_query: str = Field(
        default="",
        description="结合对话上下文还原出的完整有效查数需求"
    )

    route: Literal["seeker", "advisor"] = Field(
        default="advisor",
        description="本轮路由判定：seeker=可直接执行（语义层唯一解析、槽位齐全）；advisor=需先澄清/核验"
    )

    time_range: str = Field(
        default="",
        description="用户明确的时间范围，如 昨天、最近7天、2026-08-01至2026-08-31；未明确时留空由默认兜底"
    )

    filters: str = Field(
        default="",
        description="用户明确的口径过滤条件，如 company_category='A'；多个用 AND 连接；没有则留空"
    )

    tables: list[str] = Field(
        default_factory=list,
        description="根据元数据确定的候选表，格式为库名.表名"
    )

    fields: list[str] = Field(
        default_factory=list,
        description="根据元数据能够唯一确定的字段名"
    )

    completeness: Literal[
        "full",
        "partial",
        "none",
    ] = Field(
        default="none",
        description="completeness: full=unique, partial=ambiguous, none=unmapped"
    )

    complex: bool = Field(
        default=False,
        description="是否为复杂查询：需要窗口函数(ROW_NUMBER/RANK)、子查询、CTE 等超出平铺 GROUP BY 的 SQL 结构"
    )

    metric_mentions: list[str] = Field(
        default_factory=list,
        description="用户提到的指标业务概念，如“新增订单”“成交金额”，只写业务概念不写物理字段"
    )

    dimension_mentions: list[str] = Field(
        default_factory=list,
        description="用户提到的维度业务概念，如“经销商名称”“业务经理”"
    )

    analysis_type: str = Field(
        default="",
        description="分析类型: detail/aggregate/trend/ranking/comparison"
    )

    reason: str = Field(
        default="",
        description="确认判断和模糊度判断的主要依据"
    )

    semantic_keywords: list[str] = Field(
        default_factory=list,
        description="从用户问题拆出的语义层检索关键词（第0层输出，用于全文 grep）"
    )

    semantic_metrics: list[SemanticMetricHit] = Field(
        default_factory=list,
        description="语义层候选指标的置信度判定（第3层输出），用于分档路由"
    )

    follow_up_mode: Literal[
        "new_query",
        "result_follow_up",
        "plan_refinement",
        "clarification_explanation",
    ] = Field(
        default="new_query",
        description="连续问答类型：new_query=新查询/换话题，result_follow_up=引用上一轮结果追问，plan_refinement=沿用方案只改部分槽位，clarification_explanation=只询问口径区别"
    )

PLANNER_SYSTEM_PROMPT = """只输出纯JSON，不要markdown代码块，不要输出解释文字。

你是 Text2SQL 系统中的 Planner，负责理解用户查询意图、判断需求完整度，并通过元数据完成表字段映射。

你需要输出：
1. effective_query：当前完整有效需求
2. route：本轮路由判定（seeker=可直接执行，advisor=需先澄清/核验）
3. time_range：用户明确的时间范围
4. filters：用户明确的口径过滤条件
5. tables：候选目标表
6. fields：已确定字段
7. completeness：需求映射完整度
8. complex：是否复杂查询
9. metric_mentions：用户提到的指标业务概念
10. dimension_mentions：用户提到的维度业务概念
11. analysis_type：分析类型

禁止：
- 生成SQL
- 执行查询
- 直接修改查询方案

【effective_query规则】
effective_query 必须表达用户当前真实查询需求。

规则：
- 首次提问：保留用户原始需求
- 补充条件：合并到原需求
- 局部修改：保留未修改部分，仅替换明确修改内容
- 推翻方案：使用新目标，不继承旧错误需求
- 用户回复序号、字母、简称时，必须结合【对话历史】中上轮展示的候选编号与【最近展示候选】的字段事实还原完整含义
- 用户从候选口径中选定一个时，视为该指标口径已确认，effective_query 保留当前完整需求（含已确认的其他指标，如“新增订单数（全量）(分区维度) + 退租订单数 + 净增订单数”）
- 用户明确表示“只要某指标”“不要某指标”时，才按用户要求增删对应指标口径
- 用户明确表示“全部”“都要”时，明确保留全部指标口径

例如：
Advisor：
1. 新增用户回流订单 reflow_addition_order
2. 老用户回流订单 extend_reflow_old_order

用户：
1

应还原为：
查询新增用户回流订单，指标字段为 reflow_addition_order。

禁止将无独立含义的序号直接作为需求。
无法确定选项含义时，保留原需求，并将 completeness 判定为 partial。

【route判定规则】
route 决定本轮是否直接执行，还是先由 Advisor 澄清/核验。你是唯一路由者：
- seeker：当前有效需求的全部指标都能被语义层唯一解析（semantic_metrics 中每个指标 confidence>=0.55
  且口径唯一），时间、过滤、维度已明确（含从已有草稿继承），不需要用户补充任何信息，可直接生成方案执行。
- advisor：存在口径歧义、多个冲突候选、时间/过滤/维度缺失，需要向用户确认；
  或命中指标无法唯一解析，需要先用工具核验表/字段是否真实存在。

要点：
- 用户一次问多个指标时，只要每个指标都能唯一映射、槽位齐全，即使命中多个语义层指标也应判定 seeker。
- 多指标不等于 advisor；含糊不清、口径冲突才判 advisor。
- 不确定时判 advisor 更安全（Advisor 会继续澄清），但不要把可以确定的查询推给 Advisor。
- 结合【语义层指标候选】的置信度与【对话历史】判断，不允许仅根据关键词判断。

【time_range规则】
- 从 effective_query 或用户原话中提取明确时间范围（昨天/最近7天/某日/某区间）。
- 未明确时留空 ""，由系统默认（昨天）兜底。
- 只写业务时间范围，不要写 SQL 表达式。

【filters规则】
- 用户明确限定口径时输出过滤条件，如 company_category='A'（A类代理商）、platform='cos'。
- 多个条件用 AND 连接；没有限定留空 ""。
- 只写能确定字段名的过滤；不确定归属表也照写字段条件，表归属由语义层解析。

【元数据映射规则】
completeness：
- partial：能确定表但存在候选口径冲突，如用户说新增订单而检索结果里有 new_order, dealership_new_order, really_add_order 等多个候选，必须判 partial
- partial：部分确定，但存在候选口径冲突
- none：无法映射现有元数据

tables：
- 返回完整库名.表名
- 只能使用 metadata_context 中存在的表
- 支持多表查询，返回全部涉及表
- 无法确定返回 []

fields：
- 只能使用 metadata_context 中存在字段
- 只返回唯一确定字段
- 相似字段不得自行选择
- 禁止编造字段

tables/fields 只作为 Advisor 核验参考，最终物理字段由语义层确定性解析，不要自行编造。

原则：
不确定时保守处理；route 判定不了时选 advisor，禁止让模糊需求直接进入执行阶段。

【complex判断】
以下任一情况设置 complex=true：
- 窗口函数：排名、组内TopN等
- 子查询或嵌套分析
- CTE/WITH
- 跨粒度比较分析

普通聚合、过滤、排序不属于复杂查询。

【语义层与检索结果的优先级】
- 语义层指标候选（来源表/表达式/别名/单位）是权威业务口径，优先采信。
- 分层元数据检索结果仅用于补充物理字段发现；当两者冲突时（如字段同名但来源表不同），必须以语义层指标候选中的来源表和表达式为准，不得自行在两个来源表之间猜测或切换。
- 若语义层候选与检索结果涉及同一指标但来源表不同，按语义层候选的来源表输出，并把检索结果作为核验依据。

【metric_mentions规则】
- 提取用户提到的指标业务概念，如“新增订单”“成交金额”，只写业务概念不写物理字段名。
- 存在多个候选口径时，指标概念保持不变，物理字段由 Advisor 检索和程序门禁统一解析。
- 历史案例中的字段不能作为当前用户确认口径的证据，也不得写入 metric_mentions。
- 候选口径的含义（如“纯新用户的新增订单”）只是同一概念的候选解释，不是独立业务概念，不得写入 metric_mentions。
- 与【已确认口径】中的概念含义相同（如“新增订单数”=“新增订单”）时，必须沿用上轮原文字符串，保证跨轮概念一致，避免同义表述触发重复澄清。
- 候选展示含义只是字段的原始备注（如候选1展示为“新增订单数”），不是业务概念字符串；已确认概念“新增订单”必须逐字沿用，禁止改写成“新增订单数”。
- 反例：上轮已确认“新增订单”，即使候选展示写的是“新增订单数”，metric_mentions 仍必须输出“新增订单”；改写会导致已确认口径断链、触发重复澄清。
- 用户本轮选定单一候选口径时，保留当前完整需求中的全部业务概念（已确认与未确认概念都保留）；用户改选或明确放弃某概念后，只保留当前仍有效的概念，被替换的口径不得保留。
- 用户用已知维度枚举值限定指标时（如"A类"，见【语义层指标候选】的"可用枚举值"），
  指标概念只保留核心指标名，枚举值限定词拆入 dimension_mentions。
  例：用户说"A类新增订单" → metric_mentions=["新增订单"]、dimension_mentions=["A类"]；
  禁止把枚举值限定词并入指标概念（不得输出 ["A类新增订单"]）。
- 无法从自然语言中识别指标时返回空列表。

【dimension_mentions规则】
- 提取用户提到的维度/属性业务概念，如"经销商名称""负责人""业务经理"，只写业务概念不写物理字段名。
- "负责人""业务经理"这类展示属性属于维度概念，必须写入 dimension_mentions，禁止写入 metric_mentions。
- 与【已确认口径】中的维度概念含义相同时，同样必须逐字沿用上轮字符串（如已确认“经销商”不得改写成“经销商名称”），除非用户本轮明确使用了新表述。
- 枚举值限定词（如"A类""B类"，来自【语义层指标候选】的"可用枚举值"）属于维度概念，
  必须单独写入 dimension_mentions，禁止并入 metric_mentions。
- 无法从自然语言中识别维度时返回空列表。

【follow_up_mode规则】
- new_query：全新需求或明显换话题（与当前需求无关）。
- result_follow_up：用户引用上一轮查询结果（"第一名""这些经销商""刚才的结果"）。
- plan_refinement：沿用当前方案只修改时间、过滤、维度、排序或指标中的部分内容。
- clarification_explanation：用户只询问候选区别或解释，尚未做出选择。

【semantic_keywords规则】
- 输出从当前有效需求中提取的业务检索关键词，用于语义层全文 grep。
- 只提取与业务指标/口径相关的词（如「新增订单」「调出」「天数池」「续租」「发货」），
  剔除时间词（昨天/今天/上月/近7天）、查询动词（查询/统计/看看/分析/对比）、
  以及纯实体/维度词（经销商/平台/区域，无业务限定时剔除）。
- 每个关键词尽量短且独立可检索（如「调出明细」拆成「调出」「明细」）。
- 保留渠道/口径限定词（如「A类」「月租」），它们可能对应维度枚举值或独立指标。
- 没有可提取的业务关键词时返回空数组。

【semantic_metrics置信度判定规则】
- 结合【语义层指标候选】判断每个候选指标与用户问题的相关度，输出 id、mention、confidence：
  - confidence >= 0.9：用户说法与指标名称/别名完全一致或近义，口径唯一，直接采信并短路。
  - 0.55 <= confidence < 0.9：指标在定义/备注中相关但口径不完全确定，需要候选反问确认。
  - confidence < 0.55：指标与用户问题无关，不采信，走检索召回。
- 只输出与用户问题相关的候选；无关候选不要出现在列表里。
- 多个指标都强相关时全部输出；只有唯一强相关（top1 明显领先，差值 >= 0.15）才算语义层唯一命中。
"""

# 用户消息由 planner_node 按需拼接 sections（有内容的才带标题，避免空标题占用 token）
PLANNER_USER_TEMPLATE = """{sections}"""

METADATA_SECTION_TEMPLATE = """【分层元数据检索结果】
{metadata_context}"""

# 第0层：语义层检索关键词提取（独立小调用，避免拆词噪声进入完整解析）
PLANNER_KEYWORD_SYSTEM_PROMPT = """只输出纯JSON，不要markdown代码块，不要解释文字。

你是 Text2SQL 系统中 Planner 的检索词提取器。你的任务是从用户查询中提取用于
语义层全文检索的业务关键词（对齐语义层 grep 方式：id/name/aliases/definition/notes/dimensions 全文匹配）。

规则：
- 只提取与业务指标/口径相关的词，如「新增订单」「调出」「天数池」「续租」「退租」「发货」「库存」。
- 剔除时间词（昨天、今天、上月、近7天等）、查询动词（查询、统计、看看、分析、对比等）、
  实体/维度词（经销商、平台、区域等，无业务限定时剔除）。
- 每个关键词尽量短且独立可检索（如「调出明细」拆成「调出」「明细」两个词）。
- 保留渠道/口径限定词（如「A类」「月租」），它们可能对应维度枚举值或独立指标。
- 若没有可提取的业务关键词，返回空数组。
"""

PLANNER_KEYWORD_USER_TEMPLATE = """当前需求：
{question}

对话历史（最近N轮）：
{history}
"""
