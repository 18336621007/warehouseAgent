# Planner 阈值配置，可根据日志中的分数分布调整

# 单层高相似度候选数量阈值，超过后认为当前层级仍存在歧义
MAX_HIGH_SIMILARITY_COUNT = 3

# 判定为"高相似度"的分数门槛
HIGH_SIMILARITY_THRESHOLD = 0.65

# Planner 各层检索 k 值
TABLE_SEARCH_K = 10
COLUMN_SEARCH_K = 15

EXAMPLE_SIMILARITY_THRESHOLD = 0.7  # 优秀示例检索最低余弦相似度
