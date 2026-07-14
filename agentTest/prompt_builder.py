import json


class PromptBuilder:
    # 该类负责构造两类提示词：
    # 1. planner prompt：让模型把用户问题拆成可执行步骤
    # 2. answer prompt：让模型基于执行结果生成最终回答

    def format_schema_context(self, schema_context):
        # 将结构化的候选表/字段上下文格式化成可读文本，供 planner 参考
        if not schema_context or not schema_context.get("candidate_tables"):
            return "未找到明显相关的表结构信息。"

        lines = []
        lines.append(f"问题: {schema_context.get('query', '')}")

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

    def _format_schema_rag_context(self, schema_rag_context: list[dict]):
        # 将 schema RAG 命中的文档型上下文压缩成简洁文本，供 planner 做 grounding
        if not schema_rag_context:
            return "无可用的 schema RAG 信息。"

        lines = []
        lines.append("【Schema RAG】")

        for index, document in enumerate(schema_rag_context, start=1):
            doc_id = document.get("doc_id", "")
            table_name = document.get("table_name", "")
            summary = document.get("summary", "")
            keywords = document.get("keywords", [])

            keyword_text = ", ".join(keywords[:8])

            lines.append(f"{index}. doc_id: {doc_id}")
            lines.append(f"   table_name: {table_name}")
            lines.append(f"   summary: {summary}")
            lines.append(f"   keywords: {keyword_text}")
        return "\n".join(lines)

    def build_planner_prompt(self, query, tools, schema_context=None, schema_rag_context=None):
        # 组装 planner prompt：
        # - query：用户问题
        # - tools：系统允许使用的工具白名单
        # - schema_context：结构化候选表/字段
        # - schema_rag_context：文档型 schema 检索结果
        tools_str = json.dumps(tools, ensure_ascii=False, indent=2)
        schema_text = self.format_schema_context(schema_context)
        schema_rag_text = self._format_schema_rag_context(schema_rag_context)
        return [
            {
                "role": "system",
                "content": f"""
                你是一个 DAG Planner。
                你的任务是把用户问题拆成执行步骤（step list）。
                你必须严格输出 JSON 数组。
                ---
                规则：
                1. 每个 step 必须包含：
                   - id
                   - step（语义名称）
                   - tool（必须来自工具列表）
                   - inputs（参数）
                   - depends_on（依赖）

                2. tool 必须来自工具列表。
                3. 生成查库步骤时，优先参考提供的 Schema 上下文选择表和字段。
                4. 生成查询步骤时，优先使用 sql_query 工具，不要输出 mysql_tool，也尽量不要输出 mysql_query。
                5. 如果无法拆解，返回 []。
                ---
                工具列表：
                {tools_str}
                ---
                Schema 上下文：
                {schema_text}
                ---
                Schema RAG:
                {schema_rag_text}

                示例：
                用户：查订单
                输出：
                [
                  {{
                    "id": "s1",
                    "step": "query_orders",
                    "tool": "sql_query",
                    "inputs": {{
                      "sql": "select * from agent_order_demo limit 10"
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
        # 根据执行结果构造总结提示词，要求模型只依据真实执行结果回答
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

    def build_answer_prompt(self, state):
        # 从全局 state 中取执行 trace，复用 summary prompt 生成最终回答提示词
        return self.build_summary_prompt(state.trace)
