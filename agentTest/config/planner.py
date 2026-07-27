# Planner 阈值配置，可根据日志中的分数分布调整

# 字段层最低余弦相似度，低于此值认为字段候选不可靠
MIN_TABLE_SIMILARITY = 0.5

# 高相似度字段候选数量阈值（仅统计字段层），超过此数认为存在歧义
# full 时该阈值自动放大 3 倍作为极端兜底
MAX_HIGH_SIMILARITY_COUNT = 5

# 判定为"高相似度"的分数门槛
HIGH_SIMILARITY_THRESHOLD = 0.65

# Planner 各层检索 k 值
TABLE_SEARCH_K = 5
COLUMN_SEARCH_K = 7
