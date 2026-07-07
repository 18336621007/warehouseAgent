from agentTest.planner.plan_step import PlanStep


def validate_plan(plan):
    """
        校验并标准化计划步骤列表，过滤非法数据，统一转为PlanStep实例集合
        兼容两种输入单元：PlanStep对象 / 字典原始数据，单条解析失败会静默丢弃不中断流程

        Args:
            plan: 待校验的计划数据，预期为PlanStep/字典组成的列表；非列表则直接返回空列表

        Returns:
            list[PlanStep]: 清洗完成的合法PlanStep实例列表，仅保留解析成功的步骤
        """
    if not isinstance(plan, list):
        return []

    cleaned = []

    for item in plan:
        try:
            if isinstance(item, PlanStep):
                cleaned.append(item)
                continue

            step = PlanStep.from_dict(item)
            cleaned.append(step)
        except Exception:
            continue

    return cleaned
