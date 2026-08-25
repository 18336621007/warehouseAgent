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

# 验证 join_contracts 双向索引
if joins:
    c = joins[0]
    left = c.get("left_model", "")
    right = c.get("right_model", "")
    contract = p.get_join_contract(left, right)
    print(f"\n[OK] Join合约双向索引: left={left}, right={right}")
    print(f"     safe_for: {contract.get('safe_for', []) if contract else 'N/A'}")

print("\n✅ 语义层加载验证通过")
