# Planner 阈值配置，可根据日志中的分数分布调整

# 表层最低余弦相似度（1 - cosine_distance/2），低于此值认为无可靠候选
MIN_TABLE_SIMILARITY = 0.5

# 字段层最低余弦相似度，低于此值认为字段候选不可靠
MIN_COLUMN_SIMILARITY = 0.55

# 高相似度候选数量阈值，超过此数认为存在歧义
MAX_HIGH_SIMILARITY_COUNT = 3

# 判定为"高相似度"的分数门槛
HIGH_SIMILARITY_THRESHOLD = 0.6

# 各层检索 k 值
TABLE_SEARCH_K = 5
COLUMN_SEARCH_K = 10