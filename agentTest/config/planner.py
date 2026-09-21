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
# M2：Planner ReAct 工具调用最大步数（防止工具循环失控、token 膨胀）；放宽到 15 让模型在循环内直接完成查数，避免 execute 重试
MAX_PLANNER_TOOL_STEPS = 15
# M2：respond 但 respond_text 为空（回答未完成）时，允许 Planner 重试补齐的最大次数
MAX_PLANNER_RESPOND_RETRY = 2
# 2026-09-17：模型端 response_format JSON 偶发异常（APIError 400/5xx）时，LLM 调用的瞬时重试次数（退避 0.5s 递增）
MAX_LLM_RETRY = 2
# 2026-09-17：execute_query 多段查询的并行度（不同来源表/独立指标并行执行，出错逐级降级到串行）
MAX_QUERY_PARALLEL = 4
# M2：Seeker 执行成功但 0 行时，回 Planner 自愈的最大轮次（防死循环）
MAX_EMPTY_RESULT_ROUNDS = 2
# M3：值探查工具参数（0 行自愈时用 LIKE 实时确认字段实际取值）
PROBE_VALUES_LIMIT_DEFAULT = 20
PROBE_VALUES_LIMIT_MAX = 50

# M3/A1：单次用户输入内 Planner 发起 execute 的最大轮次（执行完回看后再查），防死循环
MAX_EXECUTION_ROUNDS = 3

# 上下文动态收敛（对齐 Codex：不写死字符阈值，全部由"剩余预算 = 窗口 − 已用 token − 输出预留"派生）
# 输出预留比例：为模型输出/定稿 JSON 预留的窗口比例
CONTEXT_BUDGET_OUTPUT_RESERVE_RATIO = 0.15
# 单个工具结果最多占剩余预算的比例（预算充足时不截断，仅超过该配额才收敛）
TOOL_RESULT_MAX_BUDGET_RATIO = 0.25
# 工具结果收敛的安全垫：最小/最大字符数（仅极端兜底，正常路径由预算比例派生）
TOOL_RESULT_MIN_CHARS = 2000
TOOL_RESULT_MAX_CHARS = 20000
# 压缩红线比例：估算下一轮输入 token 超过窗口该比例时触发上下文压缩
CONTEXT_COMPACT_REDLINE_RATIO = 0.8
# 压缩时保留的最近完整工具轮数（预算越紧保留越少，MAX→MIN 连续映射）
PLANNER_CONTEXT_KEEP_ROUNDS_MAX = 3
PLANNER_CONTEXT_KEEP_ROUNDS_MIN = 1
# 中文字符/每 token 换算系数（估算新增内容 token 占用，取偏保守值：1 token ≈ 1.5 中文字符）
CHARS_PER_TOKEN_ESTIMATE = 1.5

