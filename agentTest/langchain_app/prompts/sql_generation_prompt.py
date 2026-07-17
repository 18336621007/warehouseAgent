# SQL生成提示词模块，负责定义标准提示模版
from langchain_core.prompts import ChatPromptTemplate

def build_sql_generation_prompt():
    # 构建 SQL 生成提示模板。
    system_prompt = """
    你是一个面向数仓分析场景的 Hive SQL 助手。
    请基于提供的 schema 信息生成 Hive SQL。
    要求：
    1. 返回纯 SQL，不要输出解释说明
    2. 不要包含 markdown 代码块
    3. 不要带结尾分号
    4. 只生成只读 SQL
    5. 优先参考提供的 schema 信息选择表和字段
    6. 不要编造 schema 中不存在的字段
    7. 优先保证表名、字段名、过滤条件正确
    8. 生成的 SQL 必须尽量符合 Hive 语法
    9. 如果需要限制结果集，请显式使用 LIMIT
    10. 如果 schema 信息不足以支持生成 SQL，请明确说明信息不足，不要编造 SQL
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
