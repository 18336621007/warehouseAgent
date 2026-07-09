from agentTest.metadata.base_metadata_provider import BaseMetadataProvider


class MockMetadataProvider(BaseMetadataProvider):
    # 模拟元数据提供者，用于当前本地开发和 schema 检索测试

    def __init__(self):
        self.tables = [
            {
                "table_name": "ods_order_info",
                "table_comment": "订单明细表",
            },
            {
                "table_name": "dim_user_info",
                "table_comment": "用户维度表",
            },
        ]

        self.schemas = {
            "ods_order_info": {
                "table_name": "ods_order_info",
                "table_comment": "订单明细表",
                "columns": [
                    {"name": "order_id", "type": "bigint", "comment": "订单ID"},
                    {"name": "user_id", "type": "bigint", "comment": "用户ID"},
                    {"name": "pay_amt", "type": "decimal", "comment": "支付金额"},
                    {"name": "pay_time", "type": "string", "comment": "支付时间"},
                ],
            },
            "dim_user_info": {
                "table_name": "dim_user_info",
                "table_comment": "用户维度表",
                "columns": [
                    {"name": "user_id", "type": "bigint", "comment": "用户ID"},
                    {"name": "city", "type": "string", "comment": "城市"},
                ],
            },
        }

    def list_tables(self):
        return self.tables

    def describe_table(self, table_name: str):
        if table_name not in self.schemas:
            raise ValueError(f"table not found: {table_name}")
        return self.schemas[table_name]