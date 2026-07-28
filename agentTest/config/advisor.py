# Advisor 子图专属配置

# FAISS 各层检索 top-k
SEARCH_DB_K = 3
SEARCH_TABLE_K = 5
SEARCH_COLUMN_K = 10


# Demo 层硬止损：同一话题 Advisor 最多追问轮数
MAX_DEMO_ADVISOR_TURNS = 10

# 锁定方案前前必须先调用 search_columns  图级拦截最大重试次数（避免死循环）
MAX_COLUMN_CHECK_RETRIES = 3