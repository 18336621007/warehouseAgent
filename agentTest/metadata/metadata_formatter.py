"""
负责把原始meta整理成统一结构
表列表结构：
[
    {
        "database_name": "test",
        "table_name": "agent_order_demo",
        "table_comment": "",
        "table_type": "",
    }
]

表结构
{
    "database_name": "test",
    "table_name": "agent_order_demo",
    "table_comment": "",
    "table_type": "",
    "columns": [
        {
            "name": "order_id",
            "type": "bigint",
            "comment": "订单ID",
            "nullable": None,
            "partition_key": False,
        },
        。。。
        {}

    ]
}
"""
def format_table_item(database_name: str, table_name: str, table_comment: str = "", table_type: str = ""):
    # 标准化单个表元数据项
    return {
        "database_name": database_name,
        "table_name": table_name,
        "table_comment": table_comment or "",
        "table_type": table_type or "",
    }


def format_column_item(name: str, data_type: str, comment: str = "", nullable=None, partition_key: bool = False):
    # 标准化单个字段元数据项
    return {
        "name": name,
        "type": data_type,
        "comment": comment or "",
        "nullable": nullable,
        "partition_key": partition_key,
    }