# Evaluator LLM 自评 Prompt —— 评估对话质量和需求满足度
from pydantic import BaseModel, Field


class EvaluatorSelfScore(BaseModel):
    """LLM 自评结构化输出模型"""
    coherence_score: int = Field(
        description="语义连贯性评分(0~100)：用户问题→澄清过程→SQL→答案是否逻辑通顺，有无前后矛盾"
    )
    satisfaction_score: int = Field(
        description="需求满足度评分(0~100)：最终答案是否完全回答了用户的原始问题"
    )
    brief_comment: str = Field(
        default="",
        description="简短评语(≤30字)：一句话总结本次对话质量"
    )


EVALUATOR_SYSTEM_PROMPT = """你是一个对话质量评估助手。请根据以下信息评估本次 Text2SQL 对话的质量。

评估维度：
1. 语义连贯性(0~100)：用户原始问题 → Advisor 澄清过程 → 生成的 SQL → 最终答案，整个链路是否逻辑一致
   - 0~40：严重不一致，SQL 或答案完全偏离用户意图
   - 40~70：基本连贯，但存在小的语义偏差
   - 70~90：连贯，SQL 和答案准确回应了用户需求
   - 90~100：完美连贯，澄清高效，答案精准

2. 需求满足度(0~100)：最终答案是否完整解决了用户问题
   - 0~40：未解决或只解决了很小一部分
   - 40~70：部分解决，但缺少关键信息
   - 70~90：基本解决，答案质量良好
   - 90~100：完美解决，答案完整准确

评分标尺：
- 如果用户问题需要澄清但系统没澄清就生成了 SQL → coherence 扣分
- 如果 SQL 执行成功但答案没覆盖用户问题的所有维度 → satisfaction 扣分
- 如果整个过程流畅、答案精准 → 两项都高分

请直接给出评分，不要犹豫，客观公正即可。"""

EVALUATOR_USER_TEMPLATE = """请评估以下对话质量：

【用户原始问题】
{question}

【Planner 路由判定】
- 路由: {route}
- 原因: {planner_reason}

【Advisor 澄清轮次】{advisor_turns} 轮
{advisor_context}

【生成的 SQL】
{sql}

【最终答案】
{final_answer}

请给出评分。"""
