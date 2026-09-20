# 语义层检索工具（LLM 驱动版）：Planner ReAct 里自主 grep + 按需读文件，对齐 Codex 文件 grep 思路
# 语义层优先于 RAG：由 LLM 自己提炼核心业务词 grep，命中后 read_metric 读完整口径再推理子口径映射
# 程序只保证安全（仅访问语义层目录）、IO 与预算截断，不参与任何语义匹配/排序/兜底
import os
import re
from contextvars import ContextVar

from langchain.tools import tool
from agentTest.config.semantic import (
    SEMANTIC_GREP_TOP_K,
)
from agentTest.semantic_layer.metric_matcher import format_metric_context
from agentTest.semantic_layer.semantic_layer_provider import get_semantic_layer_provider

# 请求级已读指标文件集合：read_metric 同一文件重复读时提示直接引用，避免重复注入完整口径
_seen_metric_files: ContextVar = ContextVar("semantic_seen_files", default=None)
# 按上下文预算动态收敛：Planner 每轮按剩余预算算出语义层工具结果配额，
# 通过 ContextVar 传给工具（copy_context 并行子线程会继承），None 表示预算充足不收敛
_render_budget: ContextVar = ContextVar("semantic_render_budget", default=None)

# grep 返回限制：命中文件数上限 + 每文件命中行上限 + 每行长度（防 token 膨胀）
_GREP_FILE_LIMIT = SEMANTIC_GREP_TOP_K
_GREP_LINE_LIMIT = 6
_GREP_LINE_CHARS = 140
# 一次 grep 的关键词数量上限（防多词检索结果膨胀）
_GREP_KEYWORD_LIMIT = 5


def _norm_path(path) -> str:
    """把语义层相对路径统一为正斜杠（反斜杠转正斜杠），便于跨平台一致与 LLM 复制。"""
    return str(path or "").replace("\\", "/")

def _split_grep_keywords(text: str) -> list[str]:
    """把多关键词字符串拆成独立检索词（逗号/空格/顿号分隔，去空去重限长）。"""
    parts = [t.strip() for t in re.split(r"[,，、;；\s]+", str(text or "")) if t and t.strip()]
    seen, out = set(), []
    for part in parts:
        low = part.lower()
        if low not in seen:
            seen.add(low)
            out.append(part)
        if len(out) >= _GREP_KEYWORD_LIMIT:
            break
    return out





def set_semantic_render_budget(max_chars):
    """设置本轮语义层检索结果最大字符配额（按剩余预算派生），返回 token 供复位。"""
    return _render_budget.set(max_chars)


def reset_semantic_render_budget(token) -> None:
    """请求结束：复位渲染预算，防止跨请求串状态。"""
    _render_budget.reset(token)


def begin_semantic_dedup():
    """请求入口：开启已读指标文件集合，返回 token 供 finally 复位。"""
    return _seen_metric_files.set(set())


def end_semantic_dedup(token) -> None:
    """请求结束：复位已读文件集合，防止跨请求串状态。"""
    _seen_metric_files.reset(token)


def get_semantic_dedup_summary() -> str:
    """把已读取的指标文件清单格式化为轻量摘要，供上下文压缩时替代早期工具轮。"""
    files = _seen_metric_files.get()
    if not files:
        return ""
    lines = ["【已获取信息摘要】", "- read_metric 已读取指标文件："]
    for fp in sorted(files):
        lines.append(f"  - {fp}")
    return "\n".join(lines)


def _iter_metric_files():
    """遍历 provider 索引的全部指标（含 file_path/name），供全文 grep 定位。"""
    provider = get_semantic_layer_provider()
    for metric in provider.get_all_metrics():
        if metric.get("file_path"):
            yield metric


def _read_metric_file(metric: dict) -> str:
    """按 provider 内部 file_path 读取指标 YAML 原文（路径来自受信任索引，无穿越风险）。"""
    provider = get_semantic_layer_provider()
    full = os.path.join(provider.root_path, str(metric.get("file_path") or ""))
    try:
        with open(full, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""




# read_metric 增强：渲染指标来源表物理字段清单（字段名 + 类型 + 语义注释 + 分区标记），
# 供 Planner 自行生成 SQL 时选择字段（对齐"读到指标顺带返回来源表字段"的设计）
_FIELD_LIST_LIMIT = 50


def _format_table_fields(provider, full_name: str) -> str:
    """把指标来源表的物理字段清单格式化为文本（语义层未定义时返回空串）。"""
    if not full_name:
        return ""
    phys = provider.get_physical_table(full_name)
    if not phys:
        return ""
    fields = phys.get("fields") or {}
    partitions = set(phys.get("partition") or [])
    lines = [f"来源表 {full_name} 字段清单（生成 SQL 时只能使用以下字段名）："]
    for name, info in list(fields.items())[:_FIELD_LIST_LIMIT]:
        if isinstance(info, dict):
            ftype = str(info.get("type") or "")
            comment = str(info.get("semantic") or "")
        else:
            ftype = str(info or "")
            comment = ""
        mark = " [分区]" if name in partitions else ""
        if comment:
            lines.append(f"- {name}: {ftype}{mark}  {comment}")
        else:
            lines.append(f"- {name}: {ftype}{mark}")
    return "\n".join(lines)

def build_semantic_tools(provider=None, limit: int | None = None):
    """构建 LLM 驱动的语义层检索工具集（grep + read + list）。

    - 由 LLM 自己提炼核心业务词 grep 全文、自己判断命中文件并 read_metric 读完整口径、
      自己从 dimensional_measures/notes/枚举推理子口径映射；
    - 程序只保证安全（仅访问语义层指标目录）与预算截断，不参与语义判断；
    - 最终指标 id 由 LLM 确认后交给 execute_query 程序反查构建方案并执行。
    """
    file_limit = limit or _GREP_FILE_LIMIT

    @tool
    def grep_semantic(keywords: str) -> str:
        """在语义层指标文件中全文 grep 定位指标候选（对齐 Codex 文件 grep）。

        用核心业务短词检索（如"推广""调出""返厂"），可一次传多个词（逗号分隔）从不同角度并行定位；
        不要传整句或时间词；返回全部命中指标文件的清单（路径+名称+命中词），不设文件数上限、不展开命中行；
        命中后用 read_metric 读取完整口径。
        参数：keywords 一个或多个核心业务关键词，多个用逗号分隔。
        """
        kws = _split_grep_keywords(keywords)
        if not kws:
            return "未找到匹配结果。"
        # 逐词全文 grep，按指标文件聚合命中词与命中行
        per_metric = {}
        for kw in kws:
            for metric in _iter_metric_files():
                text = _read_metric_file(metric)
                if not text:
                    continue
                matched = []
                for ln in text.splitlines():
                    if kw in ln.lower():
                        matched.append(ln.strip()[:_GREP_LINE_CHARS])
                        if len(matched) >= _GREP_LINE_LIMIT:
                            break
                if matched:
                    mid = str(metric.get("id") or "")
                    entry = per_metric.setdefault(mid, {"metric": metric, "kw_hits": {}})
                    entry["kw_hits"][kw] = matched
        if not per_metric:
            return "未找到匹配结果。"
        # 排序优化：强命中（name/aliases 精确/子串，语义层 _score_metric_keywords）优先，
        # 再命中词数、再总命中行数；不设文件数上限（指标多也不截断候选，防漏召回）
        _sl = get_semantic_layer_provider()
        _kws_low = [str(k).lower() for k in kws]

        def _grep_sort_key(_e):
            _strong = 0
            if _sl is not None:
                _strong = _sl._score_metric_keywords(_e["metric"], _kws_low)[0]
            return (-_strong, -len(_e["kw_hits"]), -sum(len(v) for v in _e["kw_hits"].values()))

        ordered = sorted(per_metric.values(), key=_grep_sort_key)
        kw_label = "、".join(kws)
        # 清单式返回：每文件一行（路径+名称+命中词+read_metric 指引），不展开命中行，
        # 保证命中文件再多也不膨胀；细节由 LLM 按需 read_metric 读取
        out = [f'grep "{kw_label}" 命中 {len(ordered)} 个指标文件：']
        for i, entry in enumerate(ordered, start=1):
            metric = entry["metric"]
            hit_words = "、".join(entry["kw_hits"].keys())
            fp = _norm_path(metric.get("file_path", ""))
            out.append(f"{i}. {fp}（{metric.get('name', '')}, id={metric.get('id', '')}，命中: {hit_words}）→ read_metric(\"{fp}\") 读取完整口径")
        text = "\n".join(out)
        # 极端场景兜底：清单仍超预算时截断并标注可后续分段查看
        budget = _render_budget.get()
        if budget is not None and len(text) > budget:
            text = text[:budget] + "\n…（已按预算精简，可用 read_metric 读取）"
        return text

    @tool
    def read_metric(path: str) -> str:
        """读取单个指标的完整口径（含可选子口径/备注/枚举值），供确认"某说法"对应哪个指标/子口径。

        在 grep_semantic 命中后调用，参数 path 用 grep 返回的指标文件相对路径（或指标 id）；
        读取后按确认的 metric_id + dimension 调 execute_query 查数，不得臆造指标 id。
        """
        ref = str(path or "").strip()
        if not ref:
            return "未找到匹配结果。"
        provider = get_semantic_layer_provider()
        # 支持直接传指标 id
        metric = provider.get_metric_by_id(ref)
        if metric is None:
            # 支持传文件相对路径：只与 provider 内部 file_path 索引匹配，不直接打开任意路径（防穿越）
            rel_path = ref.replace("\\", "/").lstrip("/")
            if not rel_path.startswith("metrics/") or not rel_path.endswith(".yaml"):
                return "无效指标文件路径，请使用 grep_semantic 返回的相对路径。"
            for m in provider.get_all_metrics():
                if _norm_path(m.get("file_path")) == rel_path:
                    metric = m
                    break
        if metric is None:
            return "未找到匹配指标，请用 grep_semantic 重新定位。"
        # 已读去重：同一文件重复读提示直接引用，避免重复注入完整口径
        fp = _norm_path(metric.get("file_path"))
        seen = _seen_metric_files.get()
        if seen is not None:
            if fp in seen:
                return f"指标 {metric.get('id', '')} 已在上文展示完整口径，直接引用其 id 与子口径即可，无需重复读取。"
            seen.add(fp)
        budget = _render_budget.get()
        text = format_metric_context([metric], compact=budget is not None, max_chars=budget)
        # 增强：附带来源表字段清单（Planner 生成 SQL 时据此选字段，避免臆造字段名）
        _src = str(metric.get("source_model") or "")
        if _src:
            _fields_text = _format_table_fields(provider, _src)
            if _fields_text:
                text = text + "\n\n" + _fields_text
        return text

    @tool
    def list_metric_files(subject: str = "") -> str:
        """列出语义层指标文件清单（默认全部；可传 order/asset 等主题目录名）。

        没有明确关键词可 grep 时用于浏览有哪些指标，辅助定位与确认口径。
        参数：subject 指标主题目录名（如 order / asset），留空列出全部。
        """
        lines = []
        for metric in _iter_metric_files():
            if subject and str(metric.get("subject") or "") != subject:
                continue
            lines.append(f"- {metric.get('file_path', '')}（{metric.get('name', '')}）")
        if not lines:
            return "语义层暂无指标。"
        text = "语义层指标文件清单：\n" + "\n".join(lines)
        # 预算紧张时截断并标注可后续分段查看
        budget = _render_budget.get()
        if budget is not None and len(text) > budget:
            text = text[:budget] + "\n…（已按预算精简）"
        return text

    return [grep_semantic, read_metric, list_metric_files]