# LangGraph 工作流状态定义，保存各节点共享的数据。
from typing import List,Any,TypedDict


class AgentState(TypedDict, total=False):
    # 用户原始问题。
    question: str

    # 检索到的 schema 文档列表。
    schema_documents: List[Any]

    # 整理后的 schema 上下文文本。
    schema_context: str

    # 模型生成的 SQL。
    generated_sql: str

    # 表示sql是否通过校验
    sql_valid: bool

    # 记录sql校验失败原因
    sql_error: str
    # SQL 执行结果。
    sql_result: Any

    # 最终回答结果
    final_answer: str

    # 当前sql修正重试次数
    retry_count: int

    # 当前sql需要修正的原因
    sql_fix_reason: str

    # Planner 路由结果："seeker" 或 "advisor"
    route: str

    # Planner 判定原因
    planner_reason: str

    # Planner LLM 识别的实体：{"tables": [...], "fields": [...], "completeness": "full/partial/none"}
    planner_entities: dict

