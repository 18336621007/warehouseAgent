from agentTest.datasource.hive_datasource import HiveDataSource

ds = HiveDataSource()

# 先看连的是哪个库的哪张表
result = ds.query("SELECT current_database()")
print("当前库:", result)

# 再查一条确定有数据的
result = ds.query("SELECT * FROM dwm_trip.dwm_exchange_order_addition_detail_hour LIMIT 1")
print("结果:", result)