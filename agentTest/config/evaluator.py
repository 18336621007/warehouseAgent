# Evaluator 评估模块配置 —— 评分权重、阈值、复杂度预算

# 综合评分权重（总和 = 1.0）
WEIGHT_TIME = 0.1       # 话题总耗时
WEIGHT_TURNS = 0.1      # Advisor 追问轮次
WEIGHT_LLM_SELF = 0.3   # LLM 自评（语义连贯性 + 需求满足度）
WEIGHT_USER = 0.5       # 用户显式评分

# 高质量对话阈值：≥ 此分数的对话入库作为示例
HIGH_QUALITY_THRESHOLD = 80

# 用户默认评分（未打分时的中性值）
DEFAULT_USER_SCORE = 75

# ── 复杂度预算参数：期望轮次/期望耗时由查询复杂度估算，实际值在预算+容忍范围内给满分 ──
# 设计原因：用户指标多、系统一次只追问一个指标时轮次必然上升，多表/复杂查询耗时也更长，
# 写死的阈值映射会把正常的多指标澄清误判为低效，因此改为按复杂度动态预算。
# 期望轮次预算
BASE_TURNS = 1.0                 # 基础澄清机会（1 次）
TURNS_PER_METRIC = 1.0           # 每个指标概念 1 轮（一次只追问一个指标）
TURNS_PER_DIMENSION = 0.5        # 每个维度概念加成（维度通常合并处理）
TURNS_PER_EXTRA_TABLE = 0.5      # 每张额外表（多表 Join 确认）
COMPLEX_TURN_BONUS = 1.0         # 复杂查询额外轮次

# 期望耗时预算（ms）
TIME_BASE_MS = 8000              # 主链路基础耗时
TIME_PER_METRIC_MS = 4000        # 每个指标召回/澄清/解析
TIME_PER_DIMENSION_MS = 1500     # 每个维度
TIME_PER_TABLE_MS = 3000         # 每张表字段检索
TIME_PER_FIELD_MS = 800          # 每个字段
COMPLEX_TIME_MS = 5000           # 复杂查询额外耗时

# 预算评分：实际值 ≤ 预算×(1+容忍率) 给满分；超出按超支比例衰减，保底 min_score
BUDGET_TOLERANCE_RATIO = 0.5
MIN_BUDGET_SCORE = 20


def score_by_budget(actual, budget, tolerance_ratio=BUDGET_TOLERANCE_RATIO, min_score=MIN_BUDGET_SCORE):
    """按复杂度预算评分：实际 ≤ 预算×(1+容忍率) 给满分，超出按超支比例线性衰减到 min_score。"""
    allowance = max(budget, 1) * (1 + tolerance_ratio)
    if actual <= allowance:
        return 100.0
    overflow = (actual - allowance) / allowance
    return max(min_score, round(100.0 * (1 - overflow), 1))

# 优秀案例语义去重阈值：余弦相似度 ≥ 此值且表/字段一致视为同一问题
EXAMPLE_DEDUP_SIMILARITY = 0.9
