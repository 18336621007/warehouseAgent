# Planner LLM 结构化输出的 Pydantic 模型 + Prompt 模板
from typing import Literal

from pydantic import BaseModel, Field


class PlannerOutput(BaseModel):
    """Planner 对当前有效需求的模糊度分析结果。"""

    effective_query: str = Field(
        default="",
        description="结合对话上下文还原出的完整有效查数需求"
    )

    accept_locked_plan: bool = Field(
        default=False,
        description="用户是否接受 Advisor 上一轮展示的完整 locked 方案"
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
        description="元数据映射完整度"
    )

    complex: bool = Field(
        default=False,
        description="是否为复杂查询：需要窗口函数(ROW_NUMBER/RANK)、子查询、CTE 等超出平铺 GROUP BY 的 SQL 结构"
    )

    reason: str = Field(
        default="",
        description="确认判断和模糊度判断的主要依据"
    )


PLANNER_SYSTEM_PROMPT = """输出为 JSON 格式。你是 Text2SQL 系统中的 Planner，负责通过分层元数据识别感知查询需求的模糊度。

你需要完成：

1. 结合对话上下文还原当前完整有效需求 effective_query
2. 判断用户是否接受完整 locked 方案 accept_locked_plan
3. 将有效需求映射到候选表和字段
4. 判断元数据映射完整度 completeness

不要生成 SQL，不要直接修改查询方案。

## effective_query

effective_query 必须表达用户当前真正需要查询的完整业务需求。

- 首次提问：保留用户完整问题
- 用户补充条件：将补充内容合并到原需求
- 用户局部修改：保留未修改内容，只替换用户明确修改的部分
- 用户推翻原方案：使用用户新的分析目标，不再继承错误内容
- 用户回答序号、字母、字段名或简称：结合 advisor_last_answer 还原其对应的完整候选含义

例如，Advisor 上轮列出：

1. 新增用户回流订单 reflow_addition_order
2. 老用户回流订单 extend_reflow_old_order

用户回答“1”，effective_query 应表达为：

“查询新增用户回流订单，指标字段为 reflow_addition_order”。

不得把“1”“A”等无独立含义的内容直接作为有效需求。
无法确定选项对应关系时不得猜测，应保留当前需求并判定为 partial。

## accept_locked_plan

只有同时满足以下条件才能返回 true：

- 当前方案状态为 locked
- Advisor 上一轮展示了完整方案
- Advisor 正在等待最终确认
- 用户本轮明确接受整份方案
- 用户没有提出任何修改、补充或疑问

用户选择候选指标、字段、维度或序号，不属于接受完整方案。

如果用户一边表示接受、一边提出修改，返回 false。

禁止通过固定关键词机械判断，必须结合方案状态和 Advisor 上轮回复。

## 元数据映射

completeness 只能是：

- full：能够唯一确定目标表和主要字段
- partial：能确定部分内容，但仍存在多个候选口径
- none：无法映射到现有元数据

tables：

- 使用完整的“库名.表名”
- 只能使用 metadata_context 中存在的表
- 支持单表和多表查询，字段跨表时返回所有涉及的表
- 无法确定时返回空列表

fields：

- 只能使用 metadata_context 中存在的真实字段名
- 只填写能够唯一确定的字段
- 存在相似口径时不得擅自选择
- 不得编造字段

如果 accept_locked_plan=true：

- 复用 confirmed_context 中的表和字段
- 不得使用向量检索结果替换 locked 方案
- 如果当前方案不是 locked，必须返回 false

不确定时采取保守策略，不得让模糊需求进入执行阶段。


## 复杂查询判定 complex

当用户需求明显需要以下任一能力时，设置 complex=true：
- 窗口函数（如"每个渠道前3名"、"排名"、"分组内排序"）
- 子查询/嵌套查询（如"离职率最高的部门里新增订单最多的经销商"）
- CTE/WITH 子句
- 跨粒度的对比分析

普通聚合+排序不属于复杂查询（用 order_by 字段处理即可）。"""


PLANNER_USER_TEMPLATE = """
【Topic 最初问题】
{question}

【用户本轮输入】
{current_user_input}

【当前查询方案】
{confirmed_context}

【Advisor 上一轮回复】
{advisor_last_answer}

【分层元数据检索结果】
{metadata_context}

【历史相似问题】
{example_context}
"""


