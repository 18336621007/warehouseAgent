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
        description="LLM 识别出的目标字段名"
    )
    completeness: str = Field(
        default="none",
        description="映射完整度: full（库表字段都确定）/ partial（知道表但不确定字段）/ none（无法定位）"
    )
    reason: str = Field(
        default="",
        description="判定依据，一句话说明"
    )


PLANNER_SYSTEM_PROMPT = """你是一个 SQL 查询需求分析器。根据元数据信息，判断用户的自然语言问题能映射到哪些表、哪些字段。

输出规则：
1. completeness："full"（能锁定具体字段）、"partial"（知道查哪个业务但不确定字段）、"none"（和所有元数据无关）
2. tables：完整的"库.表"格式
3. fields：只填你从元数据中能确认的字段名，不要编造
4. reason：一句话说明判断依据
5. 不要输出任何额外解释，只返回结构化字段"""

PLANNER_USER_TEMPLATE = """用户问题：
{question}

可用元数据（从向量库检索到的相关表/字段信息）：
{metadata_context}

请判断该问题能映射到哪些表和字段。"""