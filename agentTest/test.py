from agentTest.validate.safe_parse_json import safe_parse_json
from agentTest.validate.validator import validate_plan


# 正常 JSON：应通过 parse 和 validate
normal_json = """
[
  {
    "id": "s1",
    "step": "query_orders",
    "tool": "mysql_query",
    "inputs": {
      "sql": "select * from ods_order_info limit 10"
    },
    "depends_on": []
  }
]
"""


# 带 markdown 的 JSON：应被 safe_parse_json 正常提取
markdown_json = """
好的，下面是执行计划：

```json
[
  {
    "id": "s1",
    "step": "query_orders",
    "tool": "mysql_query",
    "inputs": {
      "sql": "select * from ods_order_info limit 10"
    },
    "depends_on": []
  }
]
```
"""


# 缺字段 JSON：缺少 tool，应在 validate 阶段被过滤
missing_fields_json = """
[
  {
    "id": "s1",
    "step": "query_orders"
  }
]
"""


# 错工具名 JSON：tool 非法，应在 validate 阶段被过滤
wrong_tool_json = """
[
  {
    "id": "s1",
    "step": "query_orders",
    "tool": "query_db",
    "inputs": {
      "sql": "select * from ods_order_info limit 10"
    },
    "depends_on": []
  }
]
"""


# 非 JSON 文本：应在 parse 阶段返回空列表
non_json_text = """
我建议先查询订单表，再统计金额，最后汇总结果。
"""


cases = {
    "正常 JSON": {
        "raw": normal_json,
        "expected_count": 1,
        "expect_tools": ["mysql_query"],
    },
    "带 markdown 的 JSON": {
        "raw": markdown_json,
        "expected_count": 1,
        "expect_tools": ["mysql_query"],
    },
    "缺字段 JSON": {
        "raw": missing_fields_json,
        "expected_count": 0,
        "expect_tools": [],
    },
    "错工具名 JSON": {
        "raw": wrong_tool_json,
        "expected_count": 0,
        "expect_tools": [],
    },
    "非 JSON 文本": {
        "raw": non_json_text,
        "expected_count": 0,
        "expect_tools": [],
    },
}


def print_plan_steps(plan_steps):
    # 打印 validate_plan 的结果，方便观察每个 step 的结构
    if not plan_steps:
        print("validate_plan 结果: []")
        return

    print(f"validate_plan 结果数量: {len(plan_steps)}")
    for index, step in enumerate(plan_steps, start=1):
        print(f"  Step {index}:")
        print(f"    id={getattr(step, 'id', None)}")
        print(f"    name={getattr(step, 'name', None)}")
        print(f"    tool={getattr(step, 'tool', None)}")
        print(f"    inputs={getattr(step, 'inputs', None)}")
        print(f"    depends_on={getattr(step, 'depends_on', None)}")


def assert_tool_validation(case_name, validated_steps, expected_count, expect_tools):
    # 校验 step 数量是否符合预期
    actual_count = len(validated_steps)
    if actual_count != expected_count:
        raise AssertionError(
            f"[{case_name}] 期望 step 数量={expected_count}，实际={actual_count}"
        )

    # 校验 tool 结果是否符合预期
    actual_tools = [step.tool for step in validated_steps]
    if actual_tools != expect_tools:
        raise AssertionError(
            f"[{case_name}] 期望 tools={expect_tools}，实际={actual_tools}"
        )


def main():
    passed_count = 0

    for case_name, config in cases.items():
        raw_text = config["raw"]
        expected_count = config["expected_count"]
        expect_tools = config["expect_tools"]

        print("=" * 60)
        print(f"测试样例: {case_name}")
        print("-" * 60)
        print("原始输出:")
        print(raw_text.strip())

        parsed = safe_parse_json(raw_text)
        print("-" * 60)
        print("safe_parse_json 结果:")
        print(parsed)

        validated = validate_plan(parsed)
        print("-" * 60)
        print_plan_steps(validated)

        try:
            # 校验工具参数是否合法，非法 tool 不应通过 validate
            assert_tool_validation(case_name, validated, expected_count, expect_tools)
            print(f"[PASS] {case_name}")
            passed_count += 1
        except AssertionError as error:
            print(f"[FAIL] {error}")

        print()

    print("=" * 60)
    print(f"测试完成，通过 {passed_count}/{len(cases)}")


if __name__ == "__main__":
    main()
