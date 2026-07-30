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


PLANNER_SYSTEM_PROMPT = """你是一个 SQL 查询需求分析器。根据元数据信息和对话上下文，判断用户的自然语言问题能映射到哪些表、哪些字段。

## 如何使用上下文
- current_user_input：用户本轮的原始输入，可能是一个确认（"好""好的"）、一个选择（"1""A"）、一个修改（"不对""换月租"）、或一个全新问题
- confirmed_context：上一轮 Advisor 已确认的分析方案（如果有的话）
- advisor_last_answer：上一轮 Advisor 给用户的回复文本（如果有的话）。
  用户说"1""A"时，从 advisor_last_answer 推断"1"对应哪个选项，不要凭空猜测。
  例如 advisor_last_answer 里写了"1. reflow_addition_order"，用户说"1"就是选 reflow_addition_order。
- 如果 current_user_input 表达了（确认/OK/好的）且 confirmed_context 存在 → 直接复用 confirmed_context 的表和字段，completeness=full
- 如果 current_user_input 表达了（修改/调整/不对/换成/不是这个）：
  * 若用户同时给出了新的具体指标（如"不对，是新用户回流订单""换成月租口径"）→ completeness=partial，fields 填你认为最匹配的字段（帮助 Advisor 定位），但不要填 full
  * 若用户只是单纯否定未给出替代方案（如"不对""都不是"）→ completeness=partial
  * 无论如何修改 → 都不应返回 full，必须让 Advisor 重新确认
- 如果 current_user_input 是一个全新的分析需求 → 忽略 confirmed_context，completeness 按新问题评估

## 如何判断用户的简短回复（"1""A""好的"）是选择还是确认？
根据 advisor_last_answer 的内容判断 Advisor 当前处于什么阶段：

- Advisor 处于【列举选项阶段】：advisor_last_answer 中包含 "请选择"/"请确认"/"以下是"/"选项"/带编号列表（如 1. ... 2. ...）等枚举语义
  → 用户说"1""A"或简短关键词只是在选择某个候选，方案尚未最终锁定 → 必须返回 partial，不要返回 full

- Advisor 处于【等待确认阶段】：advisor_last_answer 中包含 "已锁定方案"/"确认无误"/"确认后开始查询"/"以上信息确认"等锁定语义
  → 用户说"好的""确认""可以""行"表示接受最终方案 → 返回 full，复用 confirmed_context

- 如果无法判断阶段（advisor_last_answer 为空或不明确），一律返回 partial，让 Advisor 再次确认

## 关键规则：修改 ≠ 确认
- "不对，是X"：用户在修改方案 → 必须返回 partial，让 Advisor 基于用户新指定的方向重新列出候选并确认
- "好的""可以""确认"：用户已确认 → 复用 confirmed_context，返回 full
- 绝对不要出现：用户说"不对"但你返回 full —— 这会导致跳过确认直接查询，风险极高

输出规则：
1. completeness：
   - "full"：能唯一确定要查询的具体字段（同一语义下只有一个候选，或你能明确区分选哪一个）
   - "partial"：知道查哪个表，但存在多个语义相近的字段无法确定选哪个（如同一个业务概念有多个口径：回流新增/回流老用户/回流纯新）
   - "none"：和所有元数据无关
2. tables：完整的"库.表"格式，只填你确定要用的表
3. fields：只填你能唯一确定要用的字段。如果同一语义下有多个候选（如 reflow_addition_order / extend_reflow_old_order / extend_reflow_new_order），填 partial 而不是 full
4. reason：一句话说明判断依据
5. 不要输出任何额外解释，只返回结构化字段。请以 json 格式输出"""

PLANNER_USER_TEMPLATE = """
用户问题：{question}
用户本轮实际输入：{current_user_input}
{confirmed_context}
{advisor_last_answer}

可用元数据（从向量库检索到的相关表/字段信息）：
{metadata_context}

请判断该问题能映射到哪些表和字段，返回 json 格式。

历史相似查询示例（仅供参考，辅助判断模糊度）：
{example_context}
"""
