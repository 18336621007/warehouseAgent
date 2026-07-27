# SQL生成提示词模块，负责定义标准提示模板
from langchain_core.prompts import ChatPromptTemplate

def build_sql_generation_prompt():
    # 构建 SQL 生成提示模板。
    system_prompt = """
    你是一个面向数仓分析场景的 Hive SQL 助手。
    请基于提供的 schema 信息生成 Hive SQL。
    要求：
    1. 返回纯 SQL，不要输出解释说明，不要带结尾分号，必要时使用聚合函数或窗口函数
    2. 只生成只读 SQL
    3. 优先参考提供的 schema 信息选择表和字段，不要编造 schema 中不存在的字段
    4. 优先保证表名、字段名、过滤条件正确
    5. 生成的 SQL 必须尽量符合 Hive 语法
    6. 禁止使用 bizdate、dt 等 ETL 变量占位符。时间条件请使用 date_sub(current_date, N) 或 current_date()
    7. 禁止在 SQL 中使用中文别名，字段别名必须使用英文字母、数字、下划线
    """

    human_prompt = """
        用户问题：
        {question}
        
        {confirmed_section}
        相关 schema 信息：
        {schema_context}
    """

    return ChatPromptTemplate.from_messages([
        ("system", system_prompt.strip()),
        ("human", human_prompt.strip()),
    ])
