# 语义层检索工具：Planner ReAct 里自主调用的权威口径来源
# 语义层优先于 RAG：新查询/口径确认先调 search_semantic，未命中再用 search_tables/search_columns
import re
from langchain.tools import tool
from agentTest.semantic_layer.metric_matcher import (
    grep_metrics_from_keywords,
    format_metric_context,
)

# 关键词拆分分隔符：中英文常见分隔符（对齐 metric_matcher 分词习惯）
_TOKEN_SPLIT = re.compile(r"[\s,，、。;；:：]+")


def _split_keywords(question: str) -> list[str]:
    """把查询句拆成独立业务检索词（剔除空串）。"""
    return [t.strip() for t in _TOKEN_SPLIT.split(str(question or "")) if t and t.strip()]


def build_search_semantic_tool(provider=None, limit: int = 5):
    """构建语义层指标检索工具（受控只读，返回权威口径候选文本）。

    - 复用 grep_metrics_from_keywords：对 id/name/aliases/definition/notes/dimensions 全文匹配；
    - 返回 format_metric_context 候选文本，供 LLM 判定命中指标与置信度；
    - 命中指标 id 由 LLM 在 semantic_metrics 中声明，程序用 id 反查 provider 构建方案。
    """
    @tool
    def search_semantic(question: str) -> str:
        """检索语义层业务指标候选（权威口径：来源表/表达式/维度/枚举值/备注）。

        新查询或需要确认指标口径时优先调用（语义层优先于 RAG 检索）；
        返回候选指标的名称、别名、来源表、表达式、支持维度与口径备注，供判定与方案构建参考。
        参数：question 用户查询句或业务关键词。
        """
        keywords = _split_keywords(question)
        if not keywords:
            return "未找到匹配结果。"
        matches = grep_metrics_from_keywords(keywords, provider=provider, limit=limit)
        if not matches:
            return "未找到匹配结果。"
        return format_metric_context(matches)

    return search_semantic
