# SQL生成提示词模块，负责定义标准提示模版
from langchain_core.prompts import ChatPromptTemplate

def build_sql_generation_prompt():
    # 构建 SQL 生成提示模板。
    system_prompt = """
    你是一个面向数仓分析场景的 SQL 助手。
    请基于提供的 schema 信息生成 SQL。
    要求：
    1. 只生成只读 SQL
    2. 优先参考提供的 schema 信息选择表和字段
    3. 不要编造 schema 中不存在的字段
    4. 优先保证表名、字段名、过滤条件正确
    5. 如果 schema 信息不足以支持生成 SQL，就明确说明信息不足
    6. 返回结果只包含 SQL，不要输出多余解释
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
