# Planner 阈值配置，可根据日志中的分数分布调整

# 单层高相似度候选数量阈值，超过后认为当前层级仍存在歧义
MAX_HIGH_SIMILARITY_COUNT = 3

# 判定为"高相似度"的分数门槛
HIGH_SIMILARITY_THRESHOLD = 0.65

# Planner 各层检索 k 值
TABLE_SEARCH_K = 10
COLUMN_SEARCH_K = 15

# 两段式字段召回：每张召回表内最多进入候选的字段数（全局兜底字段不额外限配额）
PER_TABLE_COLUMN_QUOTA = 4

EXAMPLE_SIMILARITY_THRESHOLD = 0.7  # 优秀示例检索最低余弦相似度
# M2：Planner ReAct 工具调用最大步数（防止工具循环失控、token 膨胀）
MAX_PLANNER_TOOL_STEPS = 4
# M2：Seeker 执行成功但 0 行时，回 Planner 自愈的最大轮次（防死循环）
MAX_EMPTY_RESULT_ROUNDS = 2
# M3：值探查工具参数（0 行自愈时用 LIKE 实时确认字段实际取值）
PROBE_VALUES_LIMIT_DEFAULT = 20
PROBE_VALUES_LIMIT_MAX = 50
