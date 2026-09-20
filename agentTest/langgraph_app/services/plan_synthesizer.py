# plan_synthesizer.py —— Planner 直达 Seeker 时的确定性方案构建
# 职责：从语义层指标命中（source_model/expression/dimensions）确定性组装查询方案，
# 只依赖语义层权威定义，不靠 LLM 猜物理字段；字段/表真实性由 lock_query_plan 校验兜底。
import re

from agentTest.semantic_layer.metric_matcher import resolve_entity_dimension_fields
from agentTest.datasource.registry import resolve_engine_candidates
from agentTest.langgraph_app.services.query_plan_service import (
    lock_query_plan,
    validate_field_table_bindings,
    _extract_time_from_filters,
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


def _extract_measure_expression(expression: str, field: str) -> str:
    """从指标表达式提取标准"聚合包裹单字段"形式的 SQL 片段（SUM/COUNT/AVG/MIN/MAX，可含 DISTINCT），
    供确定性 SQL 翻译复用；复合表达式（比率/多字段/带表别名）返回空串交由 LLM 生成。

    示例：sum({field}) → SUM(active_cabinet_num)；count(distinct delivery_no) → COUNT(DISTINCT delivery_no)。
    """
    if not expression or not field:
        return ""
    expr = str(expression).strip()
    # 替换 dimensional_measures 占位符为实际物理字段
    expr = expr.replace("{field}", field)
    m = re.match(
        r"^\s*(SUM|COUNT|AVG|MIN|MAX)\s*\(\s*(DISTINCT\s+)?%s\s*\)\s*$" % re.escape(field),
        expr,
        re.IGNORECASE,
    )
    if not m:
        return ""
    distinct = (m.group(2) or "").strip()
    agg = m.group(1).upper()
    return f"{agg}({distinct + ' ' if distinct else ''}{field})"


def _match_direct_field(word, scope_ids, sl) -> tuple:
    """维度词未命中实体时，若其本身就是候选模型的真实物理字段则直接采用（字段名直配）。

    返回 (field, table)；找不到返回 None。
    """
    w = str(word or "").strip()
    if not w:
        return None
    for model_id in scope_ids:
        fields = sl.get_table_fields(model_id) or []
        if w in fields:
            return (w, model_id)
    return None


def _resolve_dimensional_measure_field(dim_measures: list, dimension_mentions) -> str:
    """从 dimensional_measures 子项按用户指定的子口径（dimension/aliases）解析物理字段。

    匹配规则：提及词与子口径的 dimension/别名精确或互相包含（如"激活电柜"→active_cabinet_num）；
    未命中返回空串，由调用方决定失败。
    """
    mentions = [str(m) for m in (dimension_mentions or []) if str(m).strip()]
    if not mentions:
        return ""
    for item in dim_measures:
        if not isinstance(item, dict):
            continue
        _dim = str(item.get("dimension") or "")
        _aliases = [str(a) for a in (item.get("aliases") or []) if str(a).strip()]
        for _m in mentions:
            if _m == _dim or any(_m == a for a in _aliases):
                return str(item.get("field") or "")
            if _dim and (_dim in _m or _m in _dim):
                return str(item.get("field") or "")
            if any(a and (a in _m or _m in a) for a in _aliases):
                return str(item.get("field") or "")
    return ""


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

    支持两类指标：
    - 计数指标：表达式能唯一解析出单个物理字段（新增/退租/租赁等）；
    - 明细指标（query_type=detail 或 expression='*')：不聚合，无度量字段。
    续租率等分子分母复合表达式指标返回 None，交由 Advisor 澄清落草稿。
    """
    if not metric_hits:
        return None
    sl = semantic_provider.semantic_layer

    measures = []
    measure_expressions = {}
    tables = []
    field_sources = {}
    concept_resolutions = {}
    # 是否存在明细型指标（query_type=detail 或 expression='*')：无度量字段
    detail_flag = False

    # 每个指标 → 来源表 + 物理字段（语义层权威）
    for hit in metric_hits:
        src = str(hit.get("source_model") or "")
        expression = str(hit.get("expression") or "")
        if not src:
            return None
        # 明细型指标：不聚合、无度量字段，只登记来源表
        is_detail = (
            str(hit.get("query_type") or "").lower() == "detail"
            or str(expression) == "*"
        )
        if is_detail:
            detail_flag = True
        candidate_fields = set()
        model = sl.get_semantic_model(src)
        if model:
            for measure_info in (model.get("measures") or {}).values():
                if isinstance(measure_info, dict) and measure_info.get("field"):
                    candidate_fields.add(str(measure_info["field"]))
        phys = sl.get_physical_table(src)
        if phys:
            candidate_fields.update((phys.get("fields") or {}).keys())
        if is_detail:
            # 明细查询：来源表登记，无单一度量字段
            if src not in tables:
                tables.append(src)
            concept_resolutions[str(hit.get("name") or hit.get("id") or "")] = {
                "field": "*",
                "table": src,
                "source": "semantic_layer",
                "concept_type": "metric",
            }
            continue
        # dimensional_measures 型指标：表达式含 {field} 占位符，按 dimension 子口径解析物理字段
        dim_measures = hit.get("dimensional_measures") or []
        if dim_measures:
            _field = _resolve_dimensional_measure_field(dim_measures, dimension_mentions)
            fields_found = [_field] if _field else []
        else:
            fields_found = _extract_measure_fields(expression, candidate_fields)
        # 复合表达式（0 或多个物理字段）无法确定为单个度量 → 交给 Advisor
        if len(fields_found) != 1:
            return None
        field = fields_found[0]
        # 保留语义层原始聚合表达式（标准聚合包裹单字段），供确定性 SQL 翻译复用，避免每段重新让 LLM 思考
        if field not in measure_expressions:
            _expr = _extract_measure_expression(expression, field)
            if _expr:
                measure_expressions[field] = _expr
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

    # dimensional_measures 子口径选择词（如"激活电柜"）仅用于解析 {field} 占位符，
    # 不参与分组维度解析；先收集被消费的词，维度循环中跳过
    consumed_dims = set()
    for hit in metric_hits:
        if not (hit.get("dimensional_measures") or []):
            continue
        for _item in hit["dimensional_measures"]:
            if not isinstance(_item, dict):
                continue
            _dim = str(_item.get("dimension") or "")
            _aliases = [str(a) for a in (_item.get("aliases") or []) if str(a).strip()]
            for _m in (dimension_mentions or []):
                _wm = str(_m or "").strip()
                if not _wm:
                    continue
                if (_wm == _dim or any(_wm == a for a in _aliases)
                        or (_dim and (_dim in _wm or _wm in _dim))
                        or any(a and (a in _wm or _wm in a) for a in _aliases)):
                    consumed_dims.add(_wm)

    # 维度解析：业务词 → 实体 → 物理字段（分组键 + 展示字段）
    all_models = sl.get_all_semantic_models()
    scope_ids = set(tables)
    for model_id in list(scope_ids):
        for contract in sl.get_join_contracts_for_model(model_id):
            scope_ids.add(str(contract.get("left_model") or ""))
            scope_ids.add(str(contract.get("right_model") or ""))
    dimensions = []
    # 未命中实体的维度词：软失败跳过（可能是过滤值/别名/未建模维度），
    # 交由 generate_sql 结合 effective_query 与字段上下文自行判断，不阻塞方案构建
    unresolved_dimensions = []
    for word in (dimension_mentions or []):
        if str(word or "").strip() in consumed_dims:
            continue
        entity = None
        for candidate in _entity_aliases(word):
            entity = sl.get_entity_by_keyword(candidate)
            if entity:
                break
        if not entity:
            # 实体未命中时兜底：维度词本身就是候选表上的真实物理字段时直接采用
            # （避免 LLM 臆造字段被静默丢弃，如时间维度 date_day/hour）
            _direct = _match_direct_field(word, scope_ids, sl)
            if _direct:
                dim_field, dim_table = _direct
                if dim_field not in dimensions:
                    dimensions.append(dim_field)
                if dim_table not in tables:
                    tables.append(dim_table)
                field_sources.setdefault(dim_field, dim_table)
                concept_resolutions[str(word)] = {
                    "field": dim_field,
                    "table": dim_table,
                    "source": "direct_field_match",
                    "concept_type": "dimension",
                }
                continue
            unresolved_dimensions.append(word)
            continue
        entity_fields = resolve_entity_dimension_fields(
            entity,
            all_models,
            scope_model_ids=scope_ids,
            provider=sl,
        )
        if not entity_fields:
            unresolved_dimensions.append(word)
            continue
        # 优先选主表本地维度（分组键归属来源表），其次带展示字段的 entry（如经销商名称，通常落在维表），最后兜底
        chosen = None
        for entry in entity_fields:
            if str(entry.get("table") or "") in tables:
                chosen = entry
                break
        if chosen is None:
            for entry in entity_fields:
                if str(entry.get("display_field") or ""):
                    chosen = entry
                    break
        if chosen is None:
            chosen = entity_fields[0]
        dim_field = str(chosen.get("field") or "")
        dim_table = str(chosen.get("table") or "")
        if not dim_field or not dim_table:
            unresolved_dimensions.append(word)
            continue
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
        # 展示字段（如经销商名称/大区/城市）默认带出，便于阅读；归属各自实际所在表（多为维表），
        # 优先取实体配置的 display_fields 全集（去重、限数防膨胀），兼容旧的单个 display_field
        _display_items = chosen.get("display_fields") or []
        if not _display_items:
            _df0 = str(chosen.get("display_field") or "")
            _dt0 = str(chosen.get("display_table") or "") or dim_table
            if _df0 and _df0 != dim_field:
                _display_items = [{"field": _df0, "table": _dt0}]
        for _di in _display_items[:4]:
            _df = str(_di.get("field") or "")
            _dt = str(_di.get("table") or "") or dim_table
            if not _df or _df == dim_field or _df in dimensions:
                continue
            dimensions.append(_df)
            field_sources.setdefault(_df, _dt)
            if _dt and _dt not in tables:
                tables.append(_dt)

    draft = draft or {}

    # 时间字段：唯一来源是 filters 中的时间条件（不再落盘独立槽位）；
    # 这里仅用于无分区明细表安全判断与明细字段过滤
    main_table = tables[0] if tables else ""
    # 过滤：草稿确认与 Planner 槽位合并去重
    filter_parts = []
    for part in (str(draft.get("filters") or ""), filters):
        part = str(part or "").strip()
        if part and part not in filter_parts:
            filter_parts.append(part)
    final_filters = " AND ".join(filter_parts) if filter_parts else ""
    _filter_time_field, _filter_time_range = _extract_time_from_filters(final_filters)
    # 时间字段唯一来源是 filters（不再落盘独立槽位）
    time_field = _filter_time_field or ""
    # 明细查询必须由 filters 明确业务时间字段，禁止回退默认 pt_dt（避免无时间过滤全表扫描）
    if detail_flag and not time_field:
        return None

    # 过滤字段归属表（加入 field_sources 与 tables，保证 Join 规划覆盖）
    if final_filters:
        for filter_field in _extract_filter_fields(final_filters):
            if filter_field in field_sources:
                continue
            # 优先在已确认来源表内定位过滤字段，避免 pt_dt 等公共分区字段被误归到扩展维表，
            # 导致 tables 含无关联维表、确定性 SQL 翻译无法构造 JOIN
            owner_table = _find_field_table(filter_field, set(tables), sl)
            if not owner_table:
                owner_table = _find_field_table(filter_field, scope_ids, sl)
            if not owner_table:
                return None
            field_sources.setdefault(filter_field, owner_table)
            if owner_table not in tables:
                tables.append(owner_table)

    # 明细查询允许无度量字段；普通聚合方案必须至少有度量或维度
    if not measures and not dimensions and not detail_flag:
        return None
    if not tables:
        return None

    # 引擎路由：按方案主表解析引擎候选链（data_project -> doris，其余 -> trino 优先/hive 兜底）
    engine_candidates = resolve_engine_candidates(main_table) if main_table else []
    # 多表场景校验跨引擎：其余表首选引擎与主表不一致时标记，执行层拒绝跨数据源关联
    cross_engine = False
    if len(tables) > 1:
        main_primary = engine_candidates[0] if engine_candidates else ""
        for _t in tables[1:]:
            _cands = resolve_engine_candidates(_t)
            if _cands and _cands[0] != main_primary:
                cross_engine = True
                break

    plan = {
        "tables": tables,
        "engine_candidates": engine_candidates,
        "cross_engine": cross_engine,
        "measures": list(dict.fromkeys(measures)),
        "measure_expressions": measure_expressions,
        "dimensions": list(dict.fromkeys(dimensions)),
        # 查看字段 = 草稿已确认字段 + 语义层构建的度量/维度（业务方案字段）
        "select_fields": list(dict.fromkeys(
            (draft.get("select_fields") or []) + measures + dimensions
        )),
        "detail_query": detail_flag,
        "filters": final_filters,
        "field_sources": [f"{table}.{field}" for field, table in field_sources.items()],
        "order_by": list(draft.get("order_by") or []),
        "having": str(draft.get("having") or ""),
        "result_limit": int(draft.get("result_limit") or 1000),
        "complex": bool(complex_flag or draft.get("complex") or False),
        "table_plans": [],
    }
    # 明细查询补全业务展示字段：避免方案字段不全导致只查少量列（如仅有维度字段），
    # 从语义层模型口径字段收集，缺失时回退物理表字段（排除分区/时间字段）
    if detail_flag:
        detail_fields = []
        detail_model = sl.get_semantic_model(main_table)
        if detail_model:
            for _m_info in (detail_model.get("measures") or {}).values():
                if isinstance(_m_info, dict) and _m_info.get("field"):
                    _f = str(_m_info["field"])
                    if _f not in detail_fields:
                        detail_fields.append(_f)
            for _d_info in (detail_model.get("dimensions") or {}).values():
                if isinstance(_d_info, dict) and _d_info.get("field"):
                    _f = str(_d_info["field"])
                    if _f not in detail_fields:
                        detail_fields.append(_f)
        if not detail_fields:
            detail_phys = sl.get_physical_table(main_table)
            if detail_phys:
                for _f in (detail_phys.get("fields") or {}):
                    if _f in (time_field, "pt_dt"):
                        continue
                    detail_fields.append(_f)
        for _f in detail_fields:
            if _f not in plan["select_fields"]:
                plan["select_fields"].append(_f)
            field_sources.setdefault(_f, main_table)

    if unresolved_dimensions:
        # 未解析维度词写入方案供日志审计（不参与执行字段）
        plan["unresolved_dimensions"] = list(dict.fromkeys(unresolved_dimensions))
    try:
        locked = lock_query_plan(plan, concept_resolutions=concept_resolutions or None)
        # 字段-表归属确定性校验：挂错表的一律不进入执行
        if validate_field_table_bindings(locked):
            return None
        return locked
    except Exception:
        return None
