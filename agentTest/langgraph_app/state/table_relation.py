# 表关系元数据，描述两张表如何安全关联
# 关系由人工审核后启用，JoinPlanner 只使用 enabled=true 的关系
from typing import Literal, TypedDict


class TableRelation(TypedDict, total=False):
    id: str                     # 关系唯一标识，如 rel_001
    left_table: str             # 左表完整标识 database.table
    right_table: str            # 右表完整标识 database.table
    left_key: str | list[str]   # 左表关联字段名，复合键使用列表
    right_key: str | list[str]  # 右表关联字段名，复合键使用列表
    join_type: Literal["INNER", "LEFT", "RIGHT"]
    cardinality: str            # one_to_one / one_to_many / many_to_one / many_to_many
    enabled: bool               # Runtime 只读取已启用关系
    priority: int               # 多条路径时选优先级最高的
    note: str                   # 人工备注
    created_by: str             # manual / auto_infer / llm_suggest
    version: str                # 关系元数据版本