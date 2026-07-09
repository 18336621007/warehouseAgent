
import json
from config.tools import TOOLS
class PromptBuilder:
# {
#         "query": query,
#         "candidate_tables": {
#             "table_name": ...,
#             "table_comment": ...,
#             "score": ...,
#             "candidate_columns": {
#                 "name": ...,
#                 "type": ...,
#                 "comment": ...,
#                 "score": ...,
#             }
#         },
# }

    def format_schema_context(self, schema_context):
        if not schema_context or not schema_context.get("candidate_tables"):
            return "未找到明显相关的表结构信息。"

        lines = []
        lines.append(f"问题：{schema_context.get('query', '')}")

        for index, table in enumerate(schema_context.get("candidate_tables", []), start=1):
            lines.append(f"候选表{index}: {table.get('table_name', '')}")
            lines.append(f"表说明: {table.get('table_comment', '')}")
            lines.append(f"匹配分数: {table.get('score', 0)}")
            lines.append("候选字段:")

            for column in table.get("candidate_columns", []):
                lines.append(
                    f"- {column.get('name', '')} ({column.get('type', '')}) "
                    f"{column.get('comment', '')} score={column.get('score', 0)}"
                )

        return "\n".join(lines)




    def build_planner_prompt(self, query, tools, schema_context = None):
        tools_str = json.dumps(tools, ensure_ascii=False, indent=2)
        schema_text = self.format_schema_context(schema_context)
        return [
            {
                "role": "system",
                "content": f"""
                你是一个DAG Planner。
                你的任务是把用户问题拆成执行步骤（step list）。
                你必须严格输出JSON数组。
                ---
                规则：
                1. 每个step必须包含：
                   - id
                   - step（语义名称）
                   - tool（必须来自工具列表）
                   - inputs（参数）
                   - depends_on（依赖）
            
                2. tool 必须来自工具列表。
                3. 生成查库步骤时，优先参考提供的 Schema 上下文选择表和字段。
                4. 如果无法拆解，返回 []。
                ---
                工具列表：
                {tools_str}
                ---
                Schema 上下文：
                {schema_text}
            
                示例：
                用户：查订单
                输出：
                [
                  {{
                    "id": "s1",
                    "step": "query_orders",
                    "tool": "mysql_query",
                    "inputs": {{
                      "table": "ods_order_info"
                    }},
                    "depends_on": []
                  }}
                ]
                """
            },
            {
                "role": "user",
                "content": query
            }
        ]

    def build_summary_prompt(self, results):
        return [
            {
                "role": "system",
                "content": """
                你是一个数据分析助手。
                你只能根据给定的执行结果回答用户问题。
                不要编造结果。
                不要提及 step、工具名、系统内部结构。
                如果结果不足以回答问题，就明确说明。
                """
            },
            {
                "role": "user",
                "content": str(results)
            }
        ]