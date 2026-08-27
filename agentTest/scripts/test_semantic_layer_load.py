#!/usr/bin/env python3
"""验证语义层是否正确从 __file__ 所在目录加载"""
import sys
sys.path.insert(0, r"D:\code\Project\test")

from agentTest.semantic_layer.semantic_layer_provider import (
    SemanticLayerProvider,
    _DEFAULT_SEMANTIC_LAYER_ROOT,
)

print(f"[PATH] 默认语义层根目录: {_DEFAULT_SEMANTIC_LAYER_ROOT}")

p = SemanticLayerProvider()
p.load()

metrics = p.get_all_metrics()
tables = p.get_all_physical_tables()
joins = p.get_all_join_contracts()
entities = p.get_all_entities()
models = p.get_all_semantic_models()
relationships = p.get_relationships_for_model  # just check callable

print(f"[OK] 指标 (metrics):       {len(metrics)} 个")
print(f"[OK] 物理表 (physical):   {len(tables)} 张")
print(f"[OK] Join合约:             {len(joins)} 条")
print(f"[OK] 实体 (entities):      {len(entities)} 个")
print(f"[OK] 语义模型:             {len(models)} 个")

# 抽几个关键指标验证别名索引
for mid in ["addition_order_num", "renting_order_num"]:
    m = p.get_metric_by_id(mid)
    if m:
        print(f"\n[指标 {mid}]")
        print(f"  name:      {m['name']}")
        print(f"  aliases:   {m['aliases']}")
        print(f"  source:    {m['source_model']}")
        print(f"  expr:      {m['expression']}")
    else:
        print(f"[WARN] 未找到指标 {mid}")

# 验证 match_metrics_from_query
matched = p.match_metrics_from_query("昨天抖音渠道新增订单数和租赁中订单数")
print(f"\n[match_metrics_from_query] 命中 {len(matched)} 个指标")
for m in matched:
    print(f"  - {m['id']}: {m['name']}")

# 验证物理表分区字段日期格式（语义层声明 format: yyyyMMdd）
fmt = p.get_partition_field_format("dws_trip.dws_exchange_order_addition_info_hour")
print(f"\n[OK] 分区格式 pt_dt: {fmt} (期望 yyyyMMdd)")
assert fmt == "yyyyMMdd", f"分区格式异常: {fmt}"
# 兼容 physical. 前缀与未声明表默认值
fmt2 = p.get_partition_field_format("physical.dws_trip.dws_exchange_order_addition_info_hour")
assert fmt2 == "yyyyMMdd", f"physical. 前缀查询异常: {fmt2}"
fmt3 = p.get_partition_field_format("dws_trip.not_exist_table")
assert fmt3 == "yyyyMMdd", f"未声明表应回退默认 yyyyMMdd: {fmt3}"

# ── 样例验证：经销商实体 → relationship 驱动解析 ──
from agentTest.semantic_layer.metric_matcher import (
    format_metric_context,
    resolve_entity_dimension_fields,
)
dealer = p.get_entity_by_keyword("经销商")
assert dealer and dealer.get("id") == "dealer", f"经销商实体异常: {dealer}"
# 范围：指标 source_model + join 契约可达模型
_scope = {"dws_trip.dm_exchange_order_addition_info_hour"}
for _c in p.get_join_contracts_for_model("dws_trip.dm_exchange_order_addition_info_hour"):
    _scope.add(_c.get("left_model", "")); _scope.add(_c.get("right_model", ""))
dealer_fields = resolve_entity_dimension_fields(
    dealer, p.get_all_semantic_models(), scope_model_ids=_scope, provider=p,
)
print(f"\n[实体解析] 经销商(范围{len(_scope)}模型) →", [
    (f["field"], f["table"].split(".")[-1], f.get("display_field", "")) for f in dealer_fields
])
assert any(
    f["field"] == "company_id" and "dm_exchange_order_addition_info_hour" in f["table"]
    for f in dealer_fields
), "事实表 company_id 未解析（relationship 未命中）"
assert any(f.get("display_field") == "company_name" for f in dealer_fields), "维表 company_name 展示字段未解析"
# 指标支持维度（语义 key + 实体中文名渲染）
m = p.get_metric_by_id("addition_order_num")
print(f"[指标] addition_order_num dimensions={m['dimensions']}")
assert "dealer" in m["dimensions"], f"指标维度声明异常: {m['dimensions']}"
ctx = format_metric_context([m])
assert "dealer(经销商)" in ctx, f"支持维度未渲染实体中文名: {ctx}"

# 验证 join_contracts 双向索引
if joins:
    c = joins[0]
    left = c.get("left_model", "")
    right = c.get("right_model", "")
    contract = p.get_join_contract(left, right)
    print(f"\n[OK] Join合约双向索引: left={left}, right={right}")
    print(f"     safe_for: {contract.get('safe_for', []) if contract else 'N/A'}")

print("\n✅ 语义层加载验证通过")
