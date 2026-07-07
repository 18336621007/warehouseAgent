TOOLS = {
    "mysql_query": {
        "description": "查询MySQL表数据",
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
    },
    "python_tool": {
        "description": "执行Python数据处理逻辑（用于聚合、计算、join等）",
        "args_schema": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "object",
                    "description": "输入数据，来自上游step_results"
                },
                "expression": {
                    "type": "string",
                    "description": "计算逻辑描述（可选）"
                }
            },
            "required": ["data"]
        }
    }
}