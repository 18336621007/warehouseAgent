# Planner LLM 结构化输出的 Pydantic 模型 + Prompt 模板
from pydantic import BaseModel, Field


class PlannerOutput(BaseModel):
    """LLM 对用户问题的元数据映射结果"""
    tables: list[str] = Field(
        default_factory=list,
        description="LLM 识别出的目标表，格式为 库.表"
    )
    fields: list[str] = Field(
        default_factory=list,
        description="LLM 唯一确定要用的字段名，不确定的不要填"
    )
    completeness: str = Field(
        default="none",
        description="映射完整度: full（唯一确定字段）/ partial（有表但字段有多个口径未确定）/ none（无法定位）"
    )
    reason: str = Field(
        default="",
        description="判定依据，一句话说明"
    )


PLANNER_SYSTEM_PROMPT = """你是一个 SQL 查询需求分析器。根据元数据信息，判断用户的自然语言问题能映射到哪些表、哪些字段。

输出规则：
1. completeness：
   - "full"：能唯一确定要查询的具体字段（同一语义下只有一个候选，或你能明确区分选哪一个）
   - "partial"：知道查哪个表，但存在多个语义相近的字段无法确定选哪个（如同一个业务概念有多个口径：回流新增/回流老用户/回流纯新）
   - "none"：和所有元数据无关
2. tables：完整的"库.表"格式，只填你确定要用的表
3. fields：只填你能唯一确定要用的字段。如果同一语义下有多个候选（如 reflow_addition_order / extend_reflow_old_order / extend_reflow_new_order），填 partial 而不是 full
4. reason：一句话说明判断依据
5. 不要输出任何额外解释，只返回结构化字段"""

PLANNER_USER_TEMPLATE = """用户问题：
{question}

可用元数据（从向量库检索到的相关表/字段信息）：
{metadata_context}

请判断该问题能映射到哪些表和字段。"""
