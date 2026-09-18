# 语义层检索工具：Planner ReAct 里自主调用的权威口径来源
# 语义层优先于 RAG：新查询/口径确认先调 search_semantic，未命中再用 search_tables/search_columns
import re
from contextvars import ContextVar

from langchain.tools import tool
from agentTest.config.semantic import (
    SEMANTIC_GREP_TOP_K,
)
from agentTest.semantic_layer.metric_matcher import (
    grep_metric_files_from_keywords,
    format_metric_context,
)

# 请求级去重集合：记录本轮已展示过的指标 id，避免 Planner 多轮检索重复注入同一候选导致 prompt 膨胀
_seen_metric_ids: ContextVar = ContextVar("search_semantic_seen_ids", default=None)
# 按上下文预算动态收敛：Planner 每轮按剩余预算算出 search_semantic 结果配额，
# 通过 ContextVar 传给工具（copy_context 并行子线程会继承），None 表示预算充足不收敛
_render_budget: ContextVar = ContextVar("search_semantic_render_budget", default=None)


def set_semantic_render_budget(max_chars):
    """设置本轮 search_semantic 结果最大字符配额（按剩余预算派生），返回 token 供复位。"""
    return _render_budget.set(max_chars)


def reset_semantic_render_budget(token) -> None:
    """请求结束：复位渲染预算，防止跨请求串状态。"""
    _render_budget.reset(token)

# 请求级已展示指标清单（id/name/来源表）：供上下文压缩时生成轻量摘要替代早期工具轮
_seen_metric_meta: ContextVar = ContextVar("search_semantic_seen_meta", default=None)


def begin_semantic_dedup():
    """请求入口：开启新的去重集合与指标清单，返回 tokens 供 finally 复位。"""
    return (
        _seen_metric_ids.set(set()),
        _seen_metric_meta.set([]),
    )


def end_semantic_dedup(tokens) -> None:
    """请求结束：复位去重集合与指标清单，防止跨请求串状态。"""
    _seen_metric_ids.reset(tokens[0])
    _seen_metric_meta.reset(tokens[1])


def get_semantic_dedup_summary() -> str:
    """把已展示指标清单格式化为轻量摘要，供上下文压缩时替代早期工具轮。"""
    meta = _seen_metric_meta.get()
    if not meta:
        return ""
    lines = ["【已获取信息摘要】", "- search_semantic 已展示指标："]
    for m in meta:
        lines.append(
            f"  - {m.get('id', '')}({m.get('name', '')}, {m.get('source_model', '')})"
        )
    return "\n".join(lines)

# 关键词拆分分隔符：中英文常见分隔符（对齐 metric_matcher 分词习惯）
_TOKEN_SPLIT = re.compile(r"[\s,，、。;；:：]+")


def _split_keywords(question: str) -> list[str]:
    """把查询句拆成独立业务检索词（剔除空串）。"""
    return [t.strip() for t in _TOKEN_SPLIT.split(str(question or "")) if t and t.strip()]


def build_search_semantic_tool(provider=None, limit: int = 3):
    """构建语义层指标检索工具（受控只读，返回权威口径候选文本）。

    - 复用 grep_metric_files_from_keywords：文件系统 grep 只匹配 id/name/aliases（双向子串）；
    - 命中返回完整口径（含备注/枚举/来源文件），供 LLM 判定命中指标与置信度；
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
        # 多词合并检索时按词数动态放量候选数：确保每个业务词至少可能覆盖一个指标，
        # 避免固定 top-k 截断导致模型认为漏指标而分轮补搜（对齐 Codex 一次并行检索）
        _limit = max(SEMANTIC_GREP_TOP_K, len(keywords))
        matches = grep_metric_files_from_keywords(keywords, provider=provider, limit=_limit)
        if not matches:
            return "未找到匹配结果。"
        # 请求内去重：只返回未展示过的新指标，重复候选提示直接引用，避免上下文累积膨胀
        # 注意：工具在 langchain 隔离 context 中执行，ContextVar.set 新对象不回写外层，
        # 因此只修改请求入口 begin_semantic_dedup 已建好的 set 对象（引用共享）
        seen = _seen_metric_ids.get()
        if seen is None:
            # 未开启去重（独立调用场景）：退化为不去重，直接返回全部候选
            budget = _render_budget.get()
            return format_metric_context(matches, compact=budget is not None, max_chars=budget)
        new_matches = [m for m in matches if str(m.get("id") or "") not in seen]
        if not new_matches:
            names = "、".join(str(m.get("name") or m.get("id") or "") for m in matches[:3])
            return f"检索到的指标（{names} 等）已在上文展示，直接引用其 id 即可，无需重复检索。"
        for m in new_matches:
            seen.add(str(m.get("id") or ""))
            meta = _seen_metric_meta.get()
            if meta is not None:
                meta.append({
                    "id": m.get("id", ""),
                    "name": m.get("name", ""),
                    "source_model": m.get("source_model", ""),
                })
        # 完整渲染：命中后返回含备注/枚举/来源文件的完整口径（对齐 codex 打开指标文件读全文）
        budget = _render_budget.get()
        # 预算紧张时精简口径并限制长度（完整口径由 execute_query 程序反查使用）
        return format_metric_context(new_matches, compact=budget is not None, max_chars=budget)

    return search_semantic
