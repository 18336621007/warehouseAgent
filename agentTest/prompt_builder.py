
import json
from config.tools import TOOLS
class PromptBuilder:
    def build_planner_prompt(self, query, tools):
        tools_str = json.dumps(tools, ensure_ascii=False, indent=2)

        return [
            {
                "role": "system",
                "content": f"""
                你是一个DAG Planner。
            
                你的任务：
                把用户问题拆成执行步骤（step list）。
            
                你必须严格输出JSON数组。
            
                ---
            
                规则：
                1. 每个step必须包含：
                   - id
                   - step（语义名称）
                   - tool（必须来自工具列表）
                   - inputs（参数）
                   - depends_on（依赖）
            
                2. 如果无法拆解，返回：
                []
            
                3. 不允许输出解释文字
            
                ---
            
                工具列表：
                {tools_str}
            
                ---
            
                示例：
            
                用户：查订单
            
                输出：
                [
                  {{
                    "id": "s1",
                    "step": "query_orders",
                    "tool": "mysql_query",
                    "inputs": {{
                      "table": "orders"
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
                你是数据分析助手。
            
                你只能根据执行结果回答问题。
            
                不要提及系统内部结构，不要提及step。
                """
            },
            {
                "role": "user",
                "content": str(results)
            }
        ]