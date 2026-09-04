# 语义层共享配置：供 Planner、Advisor 等多组件共同引用

# 语义层唯一命中判定：配合 confidence 使用（>=0.9 才短路），top1-top2 得分差值
# 在整数词法分（10/6/3）下等价于严格领先；一般子串命中(0.7)视为部分匹配
SEMANTIC_UNIQUE_GAP_THRESHOLD: float = 0.15


# 语义层全文 grep 检索：top-k 候选数（对齐 skill 关键词 grep，命中即截断）
SEMANTIC_GREP_TOP_K: int = 5

# 语义层置信度分档（对齐 skill 置信度规则）：
# confidence >= UNIQUE  唯一强命中（名称/别名），短路跳过 FAISS 双路召回
# CANDIDATE <= confidence < UNIQUE  定义/备注弱命中，候选反问（复用现有澄清）
# confidence < CANDIDATE  视为无关，走 RAG 双路召回
SEMANTIC_CONFIDENCE_UNIQUE: float = 0.9
SEMANTIC_CONFIDENCE_CANDIDATE: float = 0.55
