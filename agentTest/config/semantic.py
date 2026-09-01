# 语义层共享配置：供 Planner、Advisor 等多组件共同引用

# 语义层唯一命中判定：配合 confidence 使用（>=0.9 才短路），top1-top2 得分差值
# 在整数词法分（10/6/3）下等价于严格领先；一般子串命中(0.7)视为部分匹配
SEMANTIC_UNIQUE_GAP_THRESHOLD: float = 0.15
