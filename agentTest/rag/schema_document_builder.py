"""
该文件负责把单表 schema 转成可供 RAG 使用的文档。
文档内容会被后续检索器和 planner 共同消费。
"""


class SchemaDocumentBuilder:
    """负责把单表 schema 转成可供 RAG 使用的文档。"""

    def build_table_document(self, table_schema: dict):
        # 提取单表基础信息，构造稳定的文档结构
        database_name = table_schema.get("database_name", "")
        table_name = table_schema.get("table_name", "")
        table_comment = table_schema.get("table_comment", "")
        columns = table_schema.get("columns", [])
        doc_id = f"hive:{database_name}.{table_name}"

        if table_comment:
            # 有表注释时优先使用表注释作为摘要
            summary = f"{database_name}.{table_name}，{table_comment}"
        else:
            # 没有表注释时用前几个字段名生成兜底摘要
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

        # 检索可用关键词包含数据库名、表名和字段名
        keywords = [database_name, table_name]

        for column in columns:
            column_name = column.get("name", "")
            if column_name:
                keywords.append(column_name)

        # 去重并去除空字符串，保证关键词列表干净
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
