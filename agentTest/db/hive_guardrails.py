# Hive 安全限制配置。
# 第一阶段以“安全优先、小结果、测试库验证”为目标，
# 先限制查询范围，避免误扫大表、误查非白名单库、执行高成本 SQL。

# 允许访问的 Hive 数据库白名单
ALLOWED_HIVE_DATABASES = {
    "test",
}

# 第一阶段要求 Hive 查询必须显式带 LIMIT
REQUIRE_LIMIT = True

# 第一阶段限制最大返回行数，防止误拉取过大结果集
MAX_HIVE_RETURN_ROWS = 100

# 第一阶段暂不允许复杂 JOIN，先把单表查询跑稳
ALLOW_JOIN = False

# 第一阶段限制只允许访问少量安全测试表
# 如果为空集合，表示当前阶段不启用表白名单，仅按库白名单控制
ALLOWED_HIVE_TABLES = {
    "agent_order_demo",
}

# 第一阶段是否允许 WITH 查询
ALLOW_WITH = True