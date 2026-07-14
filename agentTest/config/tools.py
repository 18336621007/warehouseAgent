TOOLS = {
    "sql_query": {
        "description": "只读SQL查询工具，返回标准结构化查询结果",
        "args_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "要查询的具体sql"
                }
            },
            "required": ["sql"]
        }
    }

}
