# SQL生成提示词模块，负责定义标准提示模版
from langchain_core.prompts import ChatPromptTemplate

def build_sql_generation_prompt():
    # 构建 SQL 生成提示模板。
    system_prompt = """
    你是一个面向数仓分析场景的 Hive SQL 助手。
    请基于提供的 schema 信息生成 Hive SQL。
    要求：
    1. 返回纯 SQL，不要输出解释说明,不要带结尾分号
    2. 只生成只读 SQL
    3. 优先参考提供的 schema 信息选择表和字段
    4. 不要编造 schema 中不存在的字段
    5. 优先保证表名、字段名、过滤条件正确
    6. 生成的 SQL 必须尽量符合 Hive 语法
    7. 如果需要限制结果集，请显式使用 LIMIT
    8. 无论信息是否充分，都不要返回说明文字，只返回一条尽量合理、尽量保守的 Hive SQL
    """

    human_prompt = """
        用户问题：
        {question}
    
        相关 schema 信息：
        {schema_context}
    """

    return ChatPromptTemplate.from_messages([
        ("system", system_prompt.strip()),
        ("human", human_prompt.strip()),
    ])
