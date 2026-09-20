# metric_matcher：从用户问题中匹配可能的业务指标
# 提供给 Planner/Advisor 使用，把指标上下文注入 Prompt，
# 让 LLM 优先使用语义层定义的指标（带 source_model、expression、aliases），
# 而不是凭空猜测表/字段。
from __future__ import annotations

from typing import Optional

from agentTest.semantic_layer.semantic_layer_provider import (
    SemanticLayerProvider,
    get_semantic_layer_provider,
)


def match_metrics_from_query(
    question: str,
    provider: Optional[SemanticLayerProvider] = None,
    limit: int = 5,
) -> list[dict]:
    """从用户问题匹配业务指标。

    返回按命中强度排序的指标列表，每个指标包含：
        id / name / aliases / definition / source_model / expression / unit / dimensions / notes

    使用方式：
        matched = match_metrics_from_query(user_question)
        for m in matched:
            print(m["source_model"], m["expression"])
    """
    provider = provider or get_semantic_layer_provider()
    return provider.match_metrics_from_query(question)[:limit]


def grep_metrics_from_keywords(
    keywords: list[str],
    provider: Optional[SemanticLayerProvider] = None,
    limit: int = 5,
) -> list[dict]:
    """按业务检索词全文 grep 语义层指标（含 notes/definition，对齐 skill 关键词 grep）。

    返回带 hit_type/strong_hits/weak_hits/grep_score/confidence 的指标列表；
    命中即算候选，最终置信度由 Planner LLM 判定。
    """
    provider = provider or get_semantic_layer_provider()
    return provider.grep_metrics(keywords, limit=limit)


def grep_metric_files_from_keywords(
    keywords: list[str],
    provider: Optional[SemanticLayerProvider] = None,
    limit: int = 3,
) -> list[dict]:
    """按业务检索词从文件系统 grep 定位指标（对齐 skill 的文件 grep 思路）。

    只匹配 id/name/aliases（双向子串），命中返回完整 metric dict（notes/枚举全量）
    并附加 file_path 字段；排序精确命中优先。最终置信度由 Planner LLM 判定。
    """
    provider = provider or get_semantic_layer_provider()
    return provider.grep_metric_files(keywords, limit=limit)


def resolve_metric_chain(metric_id: str, provider=None) -> dict:
    """指针导航：metric → semantic_model → physical，只打开命中指标的小组信息。"""
    provider = provider or get_semantic_layer_provider()
    return provider.resolve_metric_chain(metric_id)



def resolve_entity_dimension_fields(
    entity: dict,
    models: list[dict],
    scope_model_ids: Optional[set] = None,
    provider: Optional[SemanticLayerProvider] = None,
) -> list[dict]:
    """实体 → 候选维度字段（relationship 驱动，符合语义层规范）。

    - 分组键：候选模型里 to_entity == 实体 id 的 relationship，keys.from 即该模型的实体物理字段；
    - 展示字段：语义维度 key == 实体 id 时带出 display_field（如 company_name）；
    - 兜底：维度物理键与实体物理键一致（未语义化模型的兼容）；
    - scope_model_ids 限定候选模型（指标 source_model + join 契约可达），避免全量噪声。
    """
    if not entity:
        return []
    entity_id = str(entity.get("id") or entity.get("key") or "")
    entity_key_field = str(entity.get("key") or "")
    scope_models = models or []
    if scope_model_ids:
        scope_models = [m for m in scope_models if m.get("id") in scope_model_ids]
    results: list[dict] = []
    seen: set[tuple] = set()

    def _append(field: str, table: str, display_field: str = "") -> None:
        dedupe_key = (field, table)
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        entry = {
            "field": field,
            "table": table,
            "semantic_type": "dimension",
            "comment": f"实体{entity.get('name', '')}维度字段",
            "aliases": [],
            "score": 1.0,
        }
        if display_field:
            entry["display_field"] = display_field
        results.append(entry)

    # 1) 语义维度 key == 实体 id：带出 field 与 display_field（先执行，保证去重保留展示字段）
    for _model in scope_models:
        _model_dims = _model.get("dimensions") or {}
        if entity_id in _model_dims:
            _info = _model_dims[entity_id]
            _field = _info.get("field") if isinstance(_info, dict) else entity_id
            _display = _info.get("display_field") if isinstance(_info, dict) else ""
            _append(_field, _model.get("id", ""), _display)
    # 2) relationship 驱动：候选模型声明了到该实体的关系，keys.from 即分组物理字段
    if provider is not None:
        for _model in scope_models:
            for _rel in provider.get_relationships_for_model(_model.get("id", "")):
                if _rel.get("to_entity") == entity_id:
                    _from_key = (_rel.get("keys") or {}).get("from", "")
                    if _from_key:
                        _append(_from_key, _model.get("id", ""))
    # 3) 兜底：维度物理键与实体物理键一致（未语义化模型的兼容）
    for _model in scope_models:
        _model_dims = _model.get("dimensions") or {}
        for _dim_key, _dim_info in _model_dims.items():
            _dim_field = _dim_info.get("field") if isinstance(_dim_info, dict) else _dim_key
            if _dim_key != entity_id and _dim_field == entity_key_field:
                _append(_dim_field, _model.get("id", ""))
    return results


def _collect_metric_enum_values(provider, metric: dict) -> list[str]:
    """收集指标可达模型（source_model + join 契约两侧）中带 values 声明的维度枚举值，
    如 company_category: A类(A)、B类(B)，供 LLM 识别"修饰词→维度过滤"（如 A类→company_category=A）。

    范围收敛到指标可达模型，避免全量模型枚举值噪声；每个维度最多渲染 8 个值控制长度。
    """
    source_model = str(metric.get("source_model") or "")
    if not source_model:
        return []
    scope_ids = {source_model}
    for contract in provider.get_join_contracts_for_model(source_model):
        scope_ids.add(str(contract.get("left_model") or ""))
        scope_ids.add(str(contract.get("right_model") or ""))
    groups: dict[tuple, dict] = {}
    for model_id in scope_ids:
        model = provider.get_semantic_model(model_id)
        if not model:
            continue
        for dim_key, dim_info in (model.get("dimensions") or {}).items():
            if not isinstance(dim_info, dict):
                continue
            values = dim_info.get("values") or []
            if not values:
                continue
            field = str(dim_info.get("field") or "") or dim_key
            group = groups.setdefault(
                (model_id, dim_key),
                {"field": field, "model": model_id, "items": []},
            )
            for item in values:
                value = str(item.get("value") or "")
                if not value or any(x["value"] == value for x in group["items"]):
                    continue
                aliases = [str(a) for a in (item.get("aliases") or []) if str(a)]
                group["items"].append({
                    "label": aliases[0] if aliases else value,
                    "value": value,
                })
    parts = []
    for (model_id, dim_key), group in groups.items():
        item_strs = "、".join(
            f"{x['label']}({x['value']})" for x in group["items"][:8]
        )
        parts.append(f"{dim_key}（{group['model']}）: {item_strs}")
    return parts


def _render_expression(metric: dict) -> str:
    """明细型指标不渲染 '*'（它仅表示"明细不聚合"，对 LLM 是误导），其余保留原表达式。"""
    if str(metric.get("query_type") or "") == "detail":
        return "明细查询，无聚合表达式"
    return str(metric.get("expression") or "")


def _collect_metric_dimension_fields(provider, metric: dict) -> dict:
    """收集指标可达模型（source_model + join 契约两侧）中各逻辑维度名对应的物理字段与展示字段。

    供 format_metric_context 渲染"支持维度"时带出物理字段名，避免 LLM 照抄逻辑名
    （如 platform→pt_platform、dealer→company_id）；范围收敛到指标可达模型，查不到时调用方保留逻辑名。
    """
    source_model = str(metric.get("source_model") or "")
    if not source_model:
        return {}
    scope_ids = {source_model}
    for contract in provider.get_join_contracts_for_model(source_model):
        scope_ids.add(str(contract.get("left_model") or ""))
        scope_ids.add(str(contract.get("right_model") or ""))
    mapping = {}
    for model_id in scope_ids:
        model = provider.get_semantic_model(model_id)
        if not model:
            continue
        for dim_key, dim_info in (model.get("dimensions") or {}).items():
            if not isinstance(dim_info, dict):
                continue
            # 首次记录优先（source_model 先遍历），避免关联模型覆盖主表映射
            if dim_key not in mapping:
                mapping[dim_key] = {
                    "field": str(dim_info.get("field") or "") or dim_key,
                    "display_field": str(dim_info.get("display_field") or ""),
                }
    return mapping


def format_metric_context(metrics: list[dict], compact: bool = False, max_chars: int | None = None) -> str:
    """将匹配到的指标列表格式化为可注入 Prompt 的文本。

    字段含义：
        - source_model：指标所在表（schema.table）
        - expression：聚合表达式
        - aliases：指标的中文别名（用户可能用别名提问）
    compact=True 时精简输出：跳过长备注与枚举值（LLM 只需指标 id 与核心口径，
    完整备注/枚举由 execute_query 程序反查使用，避免候选注入过大）。
    max_chars 按上下文预算动态收敛（对齐 Codex）：超预算先降级为精简口径，仍超再截断。
    """
    if not metrics:
        return ""
    lines = ["【语义层指标候选】以下指标可能与用户问题相关，优先参考它们的来源表与口径："]
    # 语义维度 key（如 dealer）渲染时补充实体中文名（如 经销商），便于模型识别业务概念
    entity_names = {}
    for _e in get_semantic_layer_provider().get_all_entities():
        if _e.get("id"):
            entity_names[str(_e["id"])] = str(_e.get("name") or "")
    provider = get_semantic_layer_provider()
    for idx, metric in enumerate(metrics, start=1):
        aliases = ", ".join(metric.get("aliases", []) or [])
        # 维度渲染带物理字段名：逻辑名 → field（有展示字段时标注），避免 LLM 照抄逻辑名
        dim_field_map = _collect_metric_dimension_fields(provider, metric)
        _dim_parts = []
        for _d in (metric.get("dimensions", []) or []):
            _entity = entity_names.get(_d, "")
            _info = dim_field_map.get(_d)
            if _info:
                _part = f"{_d}({_entity}) → {_info['field']}" if _entity else f"{_d} → {_info['field']}"
                if _info.get("display_field"):
                    _part += f" (展示 {_info['display_field']})"
            else:
                _part = f"{_d}({_entity})" if _entity else _d
            _dim_parts.append(_part)
        dims = ", ".join(_dim_parts)
        # 兼容非字符串 note（YAML 中 "query_type: detail" 可能被解析成嵌套 dict）
        notes = " ".join(
            str(_n) if isinstance(_n, str) else " ".join(str(_v) for _v in _n.values())
            for _n in (metric.get("notes") or [])
        )
        lines.append(
            f"{idx}. {metric.get('name', '')} (id={metric.get('id', '')})\n"
            f"   别名: {aliases or '无'}\n"
            f"   来源表: {metric.get('source_model', '')}\n"
            f"   表达式: {_render_expression(metric)}\n"
            f"   单位: {metric.get('unit', '')}\n"
            f"   支持维度: {dims or '无'}\n"
            f"   定义: {metric.get('definition', '')}"
            + ("" if compact else f"\n   备注: {notes or '无'}")
        )
        # 全文 grep 命中信息：区分强命中（名称/别名）与弱命中（定义/备注），
        # 供 LLM 判定置信度（第3层）
        if metric.get("hit_type"):
            _kw = list(metric.get("strong_hits") or []) + list(metric.get("weak_hits") or [])
            lines.append(
                f"   命中: {metric.get('hit_type')}"
                f"（关键词: {', '.join(_kw) or '无'}）"
            )
        # 维度子项型指标（dimensional_measures）：列出可选子口径（中文名）与用法，
        # 让 LLM 直接用候选指标 id + dimension 表达，避免臆造指标 id
        if metric.get("dimensional_measures"):
            _sub = []
            for _dm in metric.get("dimensional_measures") or []:
                if isinstance(_dm, dict):
                    _sub.append(str(_dm.get("dimension") or ""))
            if _sub:
                lines.append(
                    "   可选子口径: " + ", ".join(_sub)
                    + f"（查询某子口径：execute_query 传 metric_id={metric.get('id', '')} 且 dimension=对应口径名）"
                )
        # 来源文件路径（文件系统 grep 定位）：供审计与后续引用
        if metric.get("file_path"):
            lines.append(f"   来源文件: {metric['file_path']}")
        # 指标可达模型的维度枚举值：供 LLM 识别"修饰词→维度过滤"，辅助拆词
        if not compact:
            enum_parts = _collect_metric_enum_values(provider, metric)
            if enum_parts:
                lines.append("   可用枚举值（可作维度过滤）:")
                for enum_line in enum_parts:
                    lines.append(f"     {enum_line}")
    text = "\n".join(lines)
    # 按上下文预算动态收敛：超预算先降级为精简口径（compact），仍超再截断并标注可反查
    if max_chars is not None and len(text) > max_chars:
        if not compact:
            return format_metric_context(metrics, compact=True, max_chars=max_chars)
        text = text[:max_chars] + "\n…（已按上下文预算精简，完整口径请按指标 id 反查）"
    return text
