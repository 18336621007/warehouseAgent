"""
{
    "doc_id": "hive:test.agent_order_demo",
    "source": "hive_metadata",
    "database_name": "test",
    "table_name": "agent_order_demo",
    "table_comment": "",
    "summary": "test.agent_order_demo，包含字段：order_id、pay_amt、pay_time、city、order_status",
    "content": "数据库: test\n表名: agent_order_demo\n表说明: 无\n字段列表:\n- order_id string 订单ID\n- pay_amt decimal(10,2) 支付金额\n- pay_time string 支付时间\n- city string 城市\n- order_status string 订单状态",
    "column_count": 5,
    "columns": [...],
    "keywords": ["test", "agent_order_demo", "order_id", "pay_amt", "pay_time", "city", "order_status"]
}
"""

class SchemaDocumentBuilder:
    """负责把单标schema转成可供RAG使用的文档"""
    def build_table_document(self, table_schema: dict):
        database_name = table_schema.get("database_name", "")
        table_name = table_schema.get("table_name", "")
        table_comment = table_schema.get("table_comment", "")
        columns = table_schema.get("columns", [])
        doc_id = f"hive:{database_name}.{table_name}"

        if table_comment:
            summary = f"{database_name}.{table_name}，{table_comment}"
        else:
            preview_columns = [column.get("name", "") for column in columns[:5]]
            preview_text = "、".join([name for name in preview_columns if name])
            summary = f"{database_name}.{table_name}，包含字段：{preview_text}"

        lines = []
        lines.append(f"数据库: {database_name}")
        lines.append(f"表名: {table_name}")
        lines.append(f"表说明: {table_comment or '无'}")
        lines.append("字段列表:")

        for column in columns:
            column_name = column.get("name", "")
            column_type = column.get("type", "")
            column_comment = column.get("comment", "")
            lines.append(f"- {column_name} {column_type} {column_comment}".strip())

        content = "\n".join(lines)


        # 检索可用，表名，数据库名，字段名
        keywords = [database_name, table_name]

        for column in columns:
            column_name = column.get("name", "")
            if column_name:
                keywords.append(column_name)
        #去重
        keywords = list(dict.fromkeys([keyword for keyword in keywords if keyword]))

        return {
            "doc_id": doc_id,
            "source": "hive_metadata",
            "database_name": database_name,
            "table_name": table_name,
            "table_comment": table_comment,
            "summary": summary,
            "content": content,
            "column_count": len(columns),
            "columns": columns,
            "keywords": keywords,
        }