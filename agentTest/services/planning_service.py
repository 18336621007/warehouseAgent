from agentTest.validate.safe_parse_json import safe_parse_json
from agentTest.validate.validator import validate_plan


class PlanningService:
    # PlanningService 负责从 query 生成 validated plan：
    # - 构造 planner prompt
    # - 调用 LLM 生成计划文本
    # - 解析并校验为标准 PlanStep 列表

    def __init__(self, llm, prompt_builder, tools):
        # 输入：
        # - llm：模型调用入口
        # - prompt_builder：planner prompt 构造器
        # - tools：系统允许的工具白名单
        # 输出：
        # - 无直接输出，实例方法 generate_plan 返回 validated plan
        self.llm = llm
        self.prompt_builder = prompt_builder
        self.tools = tools

    def generate_plan(self, query, schema_context, schema_rag_context):
        # 输入：
        # - query：用户问题
        # - schema_context：结构化候选表/字段上下文
        # - schema_rag_context：schema 文档检索上下文
        # 输出：
        # - response：模型原始计划文本
        # - validated_plan：通过校验的 PlanStep 列表
        messages = self.prompt_builder.build_planner_prompt(
            query=query,
            tools=self.tools,
            schema_context=schema_context,
            schema_rag_context=schema_rag_context
        )
        response = self.llm.chat(messages)
        plan = safe_parse_json(response)
        validated_plan = validate_plan(plan)
        return response, validated_plan
