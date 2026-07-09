# 该文件用于测试 planner 输出进入系统后的基础解析与校验流程。
# 重点覆盖 safe_parse_json 和 validate_plan 两层，确保正常 JSON、markdown JSON、非 JSON 文本在进入计划系统前被正确处理。
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


# 非 JSON 文本：应在 parse 阶段返回空列表
non_json_text = """
我建议先查询订单表，再统计金额，最后汇总结果。
"""


planner_validation_cases = {
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


def run_planner_validation_tests():
    # 执行 planner parse / validate 基础回归样例
    passed_count = 0

    print("=" * 60)
    print("开始测试组: planner_validation")
    print("=" * 60)

    for case_name, config in planner_validation_cases.items():
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
            # 校验 planner 输出经过 validate 后是否符合预期
            assert_tool_validation(case_name, validated, expected_count, expect_tools)
            print(f"[PASS] {case_name}")
            passed_count += 1
        except AssertionError as error:
            print(f"[FAIL] {error}")

        print()

    print(f"测试组 planner_validation 完成，通过 {passed_count}/{len(planner_validation_cases)}")
    print()
    return passed_count, len(planner_validation_cases)
