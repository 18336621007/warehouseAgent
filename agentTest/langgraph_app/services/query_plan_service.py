# 查询方案领域服务，集中处理方案锁定、确认和校验
from copy import deepcopy
from datetime import datetime
import re

from agentTest.langgraph_app.state.query_plan import QueryPlan
from agentTest.langgraph_app.state.query_plan import validate_query_plan
from agentTest.db.hive_guardrails import REQUIRED_FILTER_FIELDS_FOR_ALL_TABLES


def _deduplicate(values: list[str]) -> list[str]:
    """按照原顺序删除重复字段。"""
    result = []
    seen = set()

    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)

    return result



def _distribute_shared_filters(
    filters: str, source_map: dict, primary_table: str
) -> dict:
    """按过滤字段归属把全局 filters 分发到对应表：{table: filter_sql}。

    解析 "field='value' AND ..." 每段，取前导字段名，用 source_map 定位所属表；
    字段未声明或解析失败的过滤段回退主表，避免维表过滤字段被挂到主表 table_plan。
    """
    result: dict[str, list[str]] = {}
    if not filters or not filters.strip():
        return {}
    segments = re.split(r"\s+AND\s+", filters.strip(), flags=re.IGNORECASE)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        m = re.match(
            r"^(?:`?[A-Za-z_]\w*`?\.)?`?([A-Za-z_]\w*)`?\s*(?:=|<>|!=|>=|<=|>|<|IN(?:\s|\()|LIKE(?:\s|\())",
            seg,
            re.IGNORECASE,
        )
        field = m.group(1) if m else ""
        table = source_map.get(field) or primary_table
        result.setdefault(table, []).append(seg)
    return {t: " AND ".join(v) for t, v in result.items()}


def _normalize_concept_resolutions(value) -> dict:
    """把 concept_resolutions 统一为 {mention: {field, table, source}} 字典结构，
    与 lock_query_plan 写入 locked 方案的格式保持一致，
    供 Planner 改选时按 mention 反查旧字段。
    """
    if isinstance(value, dict):
        return {
            str(k): dict(v)
            for k, v in value.items()
            if isinstance(v, dict)
        }
    normalized = {}
    for item in (value or []):
        if not isinstance(item, dict):
            continue
        mention = str(
            item.get("mention") or item.get("concept") or item.get("user_intent") or ""
        )
        if not mention:
            continue
        normalized[mention] = {
            "field": str(item.get("field") or ""),
            "table": str(item.get("table") or ""),
            "source": str(
                item.get("source") or item.get("resolution_source") or "unknown"
            ),
            "concept_type": str(item.get("concept_type") or "metric"),
        }
    return normalized


def merge_draft_plan(current_plan: dict, draft_args: dict) -> QueryPlan:
    """将 Advisor 本轮 update_draft_plan 参数合并为 draft 方案：
    保留旧方案未修改部分，覆盖本轮提供的字段，作为追问中逐步完善的持久化状态。
    草稿阶段允许槽位为空，业务完整性由模型判断，字段真实性由最终提交门禁校验。
    """
    plan = deepcopy(current_plan or {})
    # 清除锁定/确认标记，重新进入草稿阶段
    plan.pop("status", None)
    plan.pop("locked_at", None)
    plan.pop("confirmed_at", None)
    plan.pop("_field_sources", None)

    list_keys = [
        "tables", "measures", "dimensions", "field_sources",
        "order_by", "table_plans", "concept_resolutions",
    ]
    scalar_keys = [
        "time_field", "time_range", "filters", "having",
        "result_limit", "complex",
    ]
    for key in list_keys:
        if key in draft_args and draft_args[key] is not None:
            value = draft_args[key]
            if key == "concept_resolutions":
                # 指标概念解析统一为字典结构，供改选时反查旧字段
                plan[key] = _normalize_concept_resolutions(value)
                continue
            plan[key] = list(value) if isinstance(value, (list, tuple)) else (
                [value] if value else []
            )
    for key in scalar_keys:
        if key in draft_args and draft_args[key] is not None:
            plan[key] = draft_args[key]

    # table 统一从 tables[0] 推导；只有 table 时反推 tables
    if plan.get("tables"):
        plan["table"] = plan["tables"][0]
    elif plan.get("table"):
        plan["tables"] = [plan["table"]]

    # fields 汇总：度量 + 维度 + 时间字段，保持去重
    fields = list(plan.get("measures") or []) + list(plan.get("dimensions") or [])
    if plan.get("time_field"):
        fields.append(plan["time_field"])
    plan["fields"] = _deduplicate(fields)

    plan["status"] = "draft"
    return plan


def lock_query_plan(proposed_plan: dict, concept_resolutions: dict = None) -> QueryPlan:
    """将 Advisor 生成的完整方案标准化为 locked 方案。table 从 tables[0] 推导。"""
    plan = deepcopy(proposed_plan)

    measures = plan.get("measures") or []
    dimensions = plan.get("dimensions") or []
    time_field = plan.get("time_field", "")
    advisors_tables = plan.get("tables") or []
    advisors_field_sources = plan.get("field_sources") or []  # ["db.table.field", ...]

    if advisors_field_sources:
        # 从 "db.table.field" 字符串提取 {field: table} 映射
        source_map: dict[str, str] = {}
        derived_tables: list[str] = []
        for fs in advisors_field_sources:
            parts = fs.rsplit(".", 1)
            if len(parts) != 2:
                continue
            table_name = parts[0]   # ads_trip.xxx
            field_name = parts[1]   # company_name
            source_map[field_name] = table_name
            if table_name not in derived_tables:
                derived_tables.append(table_name)
        plan["tables"] = derived_tables
        plan["field_sources"] = source_map                      # {field: table} dict
    else:
        plan["tables"] = advisors_tables

    # 规范化字段引用：允许 Advisor 提交 "db.table.field" 完整路径，
    # 统一拆成裸字段名并登记 field_sources，避免执行链按完整路径映射失败
    source_map = dict(plan.get("field_sources") or {})
    for ref_key in ("measures", "dimensions"):
        normalized_refs = []
        for field_ref in plan.get(ref_key) or []:
            field_ref = str(field_ref)
            parts = field_ref.rsplit(".", 1)
            if len(parts) == 2 and "." in parts[0]:
                normalized_refs.append(parts[1])
                source_map.setdefault(parts[1], parts[0])
            else:
                normalized_refs.append(field_ref)
        plan[ref_key] = _deduplicate(normalized_refs)
    if source_map:
        plan["field_sources"] = source_map
    measures = plan.get("measures") or []
    dimensions = plan.get("dimensions") or []

    # table 统一从 tables[0] 推导
    plan["table"] = plan["tables"][0] if plan["tables"] else ""

    plan.pop("_field_sources", None)

    fields = list(measures) + list(dimensions)
    if time_field:
        fields.append(time_field)
    # 过滤字段（如 A类→company_category）只用于过滤不进入 SELECT/GROUP BY，
    # 但需登记进 fields 供字段覆盖分析定位归属表，保证维表参与 Join 规划
    for _filter_field in (source_map or {}):
        if _filter_field and _filter_field not in fields:
            fields.append(_filter_field)

    plan["fields"] = _deduplicate(fields)
    # optional fields passthrough
    if "having" not in plan:
        plan["having"] = ""
    if "order_by" not in plan:
        plan["order_by"] = []
    if "result_limit" not in plan:
        plan["result_limit"] = 1000
    if "complex" not in plan:
        plan["complex"] = False
    # table_plans: 自动为所有涉及的表生成独立子方案
    advisors_table_plans = plan.get("table_plans") or []
    all_tables = plan.get("tables", [])
    default_filter_field = (
        REQUIRED_FILTER_FIELDS_FOR_ALL_TABLES[0]
        if REQUIRED_FILTER_FIELDS_FOR_ALL_TABLES
        else "pt_dt"
    )
    shared_time = plan.get("time_field") or default_filter_field
    shared_range = plan.get("time_range", "昨天")
    shared_filters = plan.get("filters", "")
    primary_table = plan.get("table", "")
    # 全局 filters 按过滤字段归属分发到对应表，未声明归属的过滤段回退主表
    _filters_by_table = _distribute_shared_filters(
        shared_filters, source_map, primary_table
    )
    advisor_plan_map = {
        table_plan.get("table", ""): table_plan
        for table_plan in advisors_table_plans
        if isinstance(table_plan, dict) and table_plan.get("table")
    }

    # 即使Advisor只提交部分table_plan，也必须为每张参与表补齐独立时间过滤计划。
    normalized_table_plans = []
    for table_name in all_tables:
        source_plan = advisor_plan_map.get(table_name) or {}
        source_filters = source_plan.get("filters", "")
        # 表自身声明的过滤优先；未声明时用全局过滤按字段归属分发的条件
        table_filters = source_filters or _filters_by_table.get(table_name, "")
        normalized_table_plans.append({
            "table": table_name,
            "time_field": source_plan.get("time_field") or shared_time,
            "time_range": source_plan.get("time_range") or shared_range,
            "filters": table_filters,
        })
    plan["table_plans"] = normalized_table_plans

    # 已解决指标写入可审计解析记录，未解决候选不允许进入锁定方案
    if concept_resolutions:
        plan["concept_resolutions"] = concept_resolutions
    plan["status"] = "locked"
    plan["locked_at"] = datetime.now().isoformat()
    plan.pop("confirmed_at", None)

    errors = validate_query_plan(plan)
    if errors:
        raise ValueError("查询方案不完整：" + "；".join(errors))

    # 硬校验：指标列表中禁止出现元数据为 dimension 的属性字段
    semantic_errors = validate_measure_semantic_types(plan)
    if semantic_errors:
        raise ValueError("查询方案指标语义错误：" + "；".join(semantic_errors))

    return plan


_TABLE_COLUMNS_INDEX = None
_SEMANTIC_TYPE_INDEX = None


def _build_field_semantic_index() -> dict:
    """懒加载 MySQL 增强元数据字段语义类型索引：{field: semantic_type}，供指标硬校验。"""
    global _SEMANTIC_TYPE_INDEX
    if _SEMANTIC_TYPE_INDEX is not None:
        return _SEMANTIC_TYPE_INDEX
    index = {}
    try:
        from agentTest.metadata.mysql_store import load_enriched_columns
        for col in load_enriched_columns():
            field = str(col.get("column_name") or "")
            ftype = str(col.get("fields_type") or "").lower()
            if field and ftype:
                index[field] = ftype
    except Exception:
        # 元数据不可用时返回空索引，校验退化为不拦截
        pass
    _SEMANTIC_TYPE_INDEX = index
    return index


def validate_measure_semantic_types(plan: dict) -> list[str]:
    """硬校验：measures 中的字段元数据语义类型若为 dimension，则拦截。

    防止“负责人/业务经理”这类属性字段被当成指标聚合；这是不依赖模型的最终防线。
    """
    measures = plan.get("measures") or []
    if not measures:
        return []
    index = _build_field_semantic_index()
    if not index:
        return []
    errors = []
    for field in measures:
        field = str(field)
        if "." in field:
            field = field.rsplit(".", 1)[-1]
        if index.get(field) == "dimension":
            errors.append(f"{field}（元数据类型=dimension，不能作为指标聚合）")
    return errors


def _build_table_columns_index() -> dict:
    """懒加载 MySQL 增强元数据索引：{database.table: set(column)}，用于字段-表归属校验。"""
    global _TABLE_COLUMNS_INDEX
    if _TABLE_COLUMNS_INDEX is not None:
        return _TABLE_COLUMNS_INDEX
    index = {}
    try:
        from agentTest.metadata.mysql_store import load_enriched_columns
        for col in load_enriched_columns():
            table_name = str(col.get("database_name") or "") + "." + str(col.get("table_name") or "")
            index.setdefault(table_name, set()).add(str(col.get("column_name") or ""))
    except Exception:
        # 元数据不可用时返回空索引，归属校验退化为不拦截（锁定链路由其他校验兜底）
        pass
    _TABLE_COLUMNS_INDEX = index
    return index


def validate_field_table_bindings(plan: dict) -> list[str]:
    """校验方案中每个字段确实存在于其声明表，返回不匹配的 "table.field" 列表。

    支持 "db.table.field" 完整路径与 {field: table} field_sources 两种来源；
    这是“程序保证元数据准确”的一部分，防止字段挂错表导致执行阶段失败。
    """
    index = _build_table_columns_index()
    if not index:
        return []
    errors = []
    # 锁定前方案可能还没有 table 键，回退到 tables[0] 作为主表归属基准
    primary_table = str(
        plan.get("table") or (plan.get("tables") or [""])[0]
    )
    declared = {}
    raw_sources = plan.get("field_sources") or []
    if isinstance(raw_sources, dict):
        # 已锁定方案：{field: table}
        declared = {
            str(field_name): str(table_name)
            for field_name, table_name in raw_sources.items()
            if field_name and table_name
        }
    elif isinstance(raw_sources, list):
        # 提交参数：["db.table.field", ...]，拆出 {field: table}
        for fs in raw_sources:
            parts = str(fs).rsplit(".", 1)
            if len(parts) == 2 and "." in parts[0]:
                declared.setdefault(parts[1], parts[0])

    for ref_key in ("measures", "dimensions"):
        for field_ref in plan.get(ref_key) or []:
            field_ref = str(field_ref)
            if "." in field_ref:
                table_name, field_name = field_ref.rsplit(".", 1)
            else:
                field_name = field_ref
                table_name = declared.get(field_name, primary_table)
            if not field_name or not table_name:
                continue
            columns = index.get(table_name)
            if columns is None or field_name not in columns:
                errors.append(f"{table_name}.{field_name}")
    return errors


def confirm_query_plan(current_plan: QueryPlan) -> QueryPlan:
    """最终确认 locked 方案，只有确认后才能交给 Seeker。"""
    if not current_plan:
        raise ValueError("当前不存在可确认的查询方案")

    if current_plan.get("status") != "locked":
        raise ValueError("只有 locked 状态的查询方案才能最终确认")

    errors = validate_query_plan(current_plan)
    if errors:
        raise ValueError("查询方案校验失败：" + "；".join(errors))

    plan = deepcopy(current_plan)
    plan["status"] = "confirmed"
    plan["confirmed_at"] = datetime.now().isoformat()
    return plan