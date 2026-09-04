# Advisor 子图专属配置

# FAISS 各层检索 top-k
SEARCH_DB_K = 3
SEARCH_TABLE_K = 3
SEARCH_COLUMN_K = 5

# BM25 混合检索权重
# - 0.0 = 只用向量检索（纯语义）
# - 1.0 = 只用 BM25 检索（纯关键词）
# - 0.3 = 30% BM25 + 70% 向量（默认，推荐）
BM25_ALPHA = 0.3

# Demo 层硬止损：同一话题 Advisor 最多追问轮数
MAX_DEMO_ADVISOR_TURNS = 10

# 锁定方案前前必须先调用 search_columns  图级拦截最大重试次数（避免死循环）
MAX_COLUMN_CHECK_RETRIES = 3

# Seeker 方案不可行（缺 join 契约/字段无归属）时，回 Planner 的修复机会次数
MAX_PLAN_REPAIR_ROUNDS = 1

# 指标歧义门禁候选收敛：只展示最相关的少量口径候选
MAX_AMBIGUITY_CANDIDATES = 6   # 澄清候选数量上限
MIN_CANDIDATE_SCORE = 0.5      # 候选相似度下限，低于该分视为不相关

# 模型受限精选：多候选未解决时精选结果下限，防止精选把歧义收敛成单一口径
RERANK_MIN_CANDIDATES = 2

# 优秀案例命中字段的排序加权：只影响候选展示顺序，不产生解析证据
EXAMPLE_FIELD_BOOST = 0.1