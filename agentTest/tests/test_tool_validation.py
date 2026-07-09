# 该文件用于测试 planner 输出中 tool 字段的合法性校验。
# 重点覆盖缺少 tool 字段、tool 名非法两类坏样例，确保这些无效 step 会在 validate 阶段被过滤，不进入后续执行链路。
from agentTest.validate.safe_parse_json import safe_parse_json
from agentTest.validate.validator import validate_plan


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


tool_validation_cases = {
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


def run_tool_validation_tests():
    # 执行 tool 字段与 tool 合法性校验样例
    passed_count = 0

    print("=" * 60)
    print("开始测试组: tool_validation")
    print("=" * 60)

    for case_name, config in tool_validation_cases.items():
        raw_text = config["raw"]
        expected_count = config["expected_count"]
        expect_tools = config["expect_tools"]

        print("-" * 60)
        print(f"测试样例: {case_name}")
        print("原始输出:")
        print(raw_text.strip())

        parsed = safe_parse_json(raw_text)
        print("【Parse阶段】safe_parse_json 结果:")
        print(parsed)

        validated = validate_plan(parsed)
        print("【Validate阶段】validate_plan 结果:")
        print_plan_steps(validated)

        try:
            # 校验工具字段是否合法，非法 tool 不应通过 validate
            assert_tool_validation(case_name, validated, expected_count, expect_tools)
            print(f"[PASS] {case_name}")
            passed_count += 1
        except AssertionError as error:
            print(f"[FAIL] {error}")

        print()

    print(f"测试组 tool_validation 完成，通过 {passed_count}/{len(tool_validation_cases)}")
    print()
    return passed_count, len(tool_validation_cases)
