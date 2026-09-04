# plan_synthesizer.py —— Planner 直达 Seeker 时的确定性方案构建
# 职责：从语义层指标命中（source_model/expression/dimensions）确定性组装查询方案，
# 只依赖语义层权威定义，不靠 LLM 猜物理字段；字段/表真实性由 lock_query_plan 校验兜底。
import re

from agentTest.semantic_layer.metric_matcher import resolve_entity_dimension_fields
from agentTest.langgraph_app.services.query_plan_service import (
    lock_query_plan,
    validate_field_table_bindings,
)

# 表达式解析时剔除的 SQL 关键字/函数名，避免被误判为物理字段
_SQL_FUNC_STOP = {
    "sum", "if", "round", "count", "avg", "min", "max", "coalesce",
    "distinct", "null", "then", "else", "end", "case", "when", "and",
    "or", "not", "is", "in", "between", "like", "abs", "floor", "ceil",
    "date_sub", "current_date", "date_format", "datediff", "greatest",
    "least", "nullif", "concat", "substr", "cast", "upper", "lower",
}


def _extract_measure_fields(expression: str, candidate_fields: set) -> list[str]:
    """从指标表达式提取真实物理字段（限定在候选字段集合内，保序去重）。"""
    if not expression:
        return []
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expression)
    result = []
    for token in tokens:
        if token in _SQL_FUNC_STOP or token not in candidate_fields:
            continue
        if token not in result:
            result.append(token)
    return result


def _entity_aliases(word: str) -> list[str]:
    """生成实体匹配候选词：先原词，再去掉展示属性后缀。"""
    candidates = [word]
    stripped = re.sub(r"(名称|名字|编号|编码|代码|id|ID|标识)$", "", str(word or "").strip())
    if stripped and stripped != word:
        candidates.append(stripped)
    return candidates


def _find_field_table(field: str, scope_ids: set, sl) -> str:
    """在可达模型/物理表内定位过滤字段的归属表。"""
    for model_id in scope_ids:
        model = sl.get_semantic_model(model_id)
        if not model:
            continue
        for dim_key, dim_info in (model.get("dimensions") or {}).items():
            if isinstance(dim_info, dict) and str(dim_info.get("field") or "") == field:
                return model_id
        for measure_info in (model.get("measures") or {}).values():
            if isinstance(measure_info, dict) and str(measure_info.get("field") or "") == field:
                return model_id
    for model_id in scope_ids:
        phys = sl.get_physical_table(model_id)
        if phys and field in (phys.get("fields") or {}):
            return model_id
    return ""


def _extract_filter_fields(filters: str) -> list[str]:
    """从过滤条件字符串解析前导字段名。"""
    if not filters:
        return []
    fields = []
    for segment in re.split(r"\s+AND\s+", filters, flags=re.IGNORECASE):
        m = re.match(
            r"^\s*`?([A-Za-z_]\w*)`?\s*(?:=|<>|!=|>=|<=|>|<|IN(?:\s|\()|LIKE(?:\s|\())",
            segment,
            re.IGNORECASE,
        )
        if m and m.group(1) not in fields:
            fields.append(m.group(1))
    return fields


def build_plan_from_semantic(
    metric_hits,
    semantic_provider,
    dimension_mentions=None,
    time_range="",
    filters="",
    draft=None,
    complex_flag=False,
    concept_resolutions=None,
):
    """从语义层指标命中确定性构建完整查询方案，失败返回 None（由 Planner 降级 Advisor）。

    只支持表达式能唯一解析出单个物理字段的指标（新增/退租/租赁等计数指标）；
    续租率等分子分母复合表达式指标返回 None，交由 Advisor 澄清落草稿。
    """
    if not metric_hits:
        return None
    sl = semantic_provider.semantic_layer

    measures = []
    tables = []
    field_sources = {}
    concept_resolutions = {}

    # 每个指标 → 来源表 + 物理字段（语义层权威）
    for hit in metric_hits:
        src = str(hit.get("source_model") or "")
        expression = str(hit.get("expression") or "")
        if not src:
            return None
        candidate_fields = set()
        model = sl.get_semantic_model(src)
        if model:
            for measure_info in (model.get("measures") or {}).values():
                if isinstance(measure_info, dict) and measure_info.get("field"):
                    candidate_fields.add(str(measure_info["field"]))
        phys = sl.get_physical_table(src)
        if phys:
            candidate_fields.update((phys.get("fields") or {}).keys())
        fields_found = _extract_measure_fields(expression, candidate_fields)
        # 复合表达式（0 或多个物理字段）无法确定为单个度量 → 交给 Advisor
        if len(fields_found) != 1:
            return None
        field = fields_found[0]
        if field not in measures:
            measures.append(field)
        if src not in tables:
            tables.append(src)
        field_sources.setdefault(field, src)
        # 可审计的指标解析证据：语义层权威口径
        concept_resolutions[str(hit.get("name") or hit.get("id") or "")] = {
            "field": field,
            "table": src,
            "source": "semantic_layer",
            "concept_type": "metric",
        }

    # 维度解析：业务词 → 实体 → 物理字段（分组键 + 展示字段）
    all_models = sl.get_all_semantic_models()
    scope_ids = set(tables)
    for model_id in list(scope_ids):
        for contract in sl.get_join_contracts_for_model(model_id):
            scope_ids.add(str(contract.get("left_model") or ""))
            scope_ids.add(str(contract.get("right_model") or ""))
    dimensions = []
    for word in (dimension_mentions or []):
        entity = None
        for candidate in _entity_aliases(word):
            entity = sl.get_entity_by_keyword(candidate)
            if entity:
                break
        if not entity:
            return None
        entity_fields = resolve_entity_dimension_fields(
            entity,
            all_models,
            scope_model_ids=scope_ids,
            provider=sl,
        )
        if not entity_fields:
            return None
        # 优先选来源表上的本地维度字段；跨表时取第一个可达结果
        chosen = None
        for entry in entity_fields:
            if str(entry.get("table") or "") in tables:
                chosen = entry
                break
        if chosen is None:
            chosen = entity_fields[0]
        dim_field = str(chosen.get("field") or "")
        dim_table = str(chosen.get("table") or "")
        if not dim_field or not dim_table:
            return None
        if dim_field not in dimensions:
            dimensions.append(dim_field)
        if dim_table not in tables:
            tables.append(dim_table)
        field_sources.setdefault(dim_field, dim_table)
        concept_resolutions[str(entity.get("name") or word)] = {
            "field": dim_field,
            "table": dim_table,
            "source": "semantic_layer",
            "concept_type": "dimension",
        }
        # 展示字段（如经销商名称）默认带出，便于阅读
        display_field = str(chosen.get("display_field") or "")
        if display_field and display_field != dim_field and display_field not in dimensions:
            dimensions.append(display_field)
            field_sources.setdefault(display_field, dim_table)

    draft = draft or {}

    # 时间：草稿确认 > Planner 槽位 > 默认
    main_table = tables[0] if tables else ""
    time_field = (
        str(draft.get("time_field") or "")
        or semantic_provider.get_table_time_field(main_table)
        or "pt_dt"
    )
    final_time_range = str(draft.get("time_range") or "") or time_range or "昨天"

    # 过滤：草稿确认与 Planner 槽位合并去重
    filter_parts = []
    for part in (str(draft.get("filters") or ""), filters):
        part = str(part or "").strip()
        if part and part not in filter_parts:
            filter_parts.append(part)
    final_filters = " AND ".join(filter_parts) if filter_parts else ""

    # 过滤字段归属表（加入 field_sources 与 tables，保证 Join 规划覆盖）
    if final_filters:
        for filter_field in _extract_filter_fields(final_filters):
            if filter_field in field_sources:
                continue
            owner_table = _find_field_table(filter_field, scope_ids, sl)
            if not owner_table:
                return None
            field_sources.setdefault(filter_field, owner_table)
            if owner_table not in tables:
                tables.append(owner_table)

    if not measures and not dimensions:
        return None
    if not tables:
        return None

    plan = {
        "tables": tables,
        "measures": list(dict.fromkeys(measures)),
        "dimensions": list(dict.fromkeys(dimensions)),
        "time_field": time_field,
        "time_range": final_time_range,
        "filters": final_filters,
        "field_sources": [f"{table}.{field}" for field, table in field_sources.items()],
        "order_by": list(draft.get("order_by") or []),
        "having": str(draft.get("having") or ""),
        "result_limit": int(draft.get("result_limit") or 1000),
        "complex": bool(complex_flag or draft.get("complex") or False),
        "table_plans": [],
    }
    try:
        locked = lock_query_plan(plan, concept_resolutions=concept_resolutions or None)
        # 字段-表归属确定性校验：挂错表的一律不进入执行
        if validate_field_table_bindings(locked):
            return None
        return locked
    except Exception:
        return None


def finalize_draft_plan(draft):
    """Advisor 已落盘完整草稿（如复合指标）时，校验后直接收尾为 locked 方案。"""
    if not draft:
        return None
    try:
        locked = lock_query_plan(
            dict(draft),
            concept_resolutions=draft.get("concept_resolutions"),
        )
        if validate_field_table_bindings(locked):
            return None
        return locked
    except Exception:
        return None
