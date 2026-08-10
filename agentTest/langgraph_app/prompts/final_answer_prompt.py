# 最终答案组装提示词：严格基于 SQL 执行结果回答
FINAL_ANSWER_SYSTEM_PROMPT = "你是一个数据分析助手。请严格基于提供的 SQL 查询结果回答用户问题，不要编造信息。"
FINAL_ANSWER_HUMAN_TEMPLATE = "用户问题：\n{question}\n\nSQL 执行结果：\n{sql_result}"
