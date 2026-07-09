# 该文件用于测试 SQL 安全校验规则，确保只读查询能通过，写操作、多语句和空 SQL 会被拦截。
from agentTest.validate.sql_validate import is_read_only_sql


sql_validation_cases = {
    "合法 SELECT": {
        "sql": "select * from ods_order_info limit 10",
        "expected_valid": True,
    },
    "合法 WITH 查询": {
        "sql": "with t as (select * from ods_order_info) select * from t limit 10",
        "expected_valid": True,
    },
    "空 SQL": {
        "sql": "   ",
        "expected_valid": False,
    },
    "DELETE 语句": {
        "sql": "delete from ods_order_info where id = 1",
        "expected_valid": False,
    },
    "UPDATE 语句": {
        "sql": "update ods_order_info set pay_amt = 0 where id = 1",
        "expected_valid": False,
    },
    "DROP 语句": {
        "sql": "drop table ods_order_info",
        "expected_valid": False,
    },
    "多语句拼接": {
        "sql": "select * from ods_order_info; delete from dim_user_info",
        "expected_valid": False,
    },
}


def run_sql_validation_tests():
    # 执行 SQL 安全校验样例，防止非法 SQL 进入数据库执行链路
    passed_count = 0

    print("=" * 60)
    print("开始测试组: sql_validation")
    print("=" * 60)

    for case_name, config in sql_validation_cases.items():
        sql = config["sql"]
        expected_valid = config["expected_valid"]

        print("-" * 60)
        print(f"测试样例: {case_name}")
        print(f"SQL: {sql}")

        actual_valid, message = is_read_only_sql(sql)
        print(f"校验结果: valid={actual_valid}, message={message}")

        if actual_valid == expected_valid:
            print(f"[PASS] {case_name}")
            passed_count += 1
        else:
            print(
                f"[FAIL] [{case_name}] 期望 valid={expected_valid}，实际 valid={actual_valid}"
            )

        print()

    print(f"测试组 sql_validation 完成，通过 {passed_count}/{len(sql_validation_cases)}")
    print()
    return passed_count, len(sql_validation_cases)
