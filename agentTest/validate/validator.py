# 导入计划步骤实体类
from agentTest.planner.plan_step import PlanStep


def validate_plan(plan):
    """
        校验并标准化计划步骤列表，过滤非法数据，统一转为PlanStep实例集合
        兼容两种输入单元：PlanStep对象 / 字典原始数据，单条解析失败会静默丢弃不中断流程

        Args:
            plan: 待校验的计划数据，预期为PlanStep/字典组成的列表；非列表则直接返回空列表

        Returns:
            list[PlanStep]: 清洗完成的合法PlanStep实例列表，仅保留解析成功、ID唯一、依赖合法的步骤
        """
    # 入参类型校验：输入非列表直接返回空结果
    if not isinstance(plan, list):
        return []

    # 存储解析成功的PlanStep实例临时容器
    cleaned = []

    # 遍历计划内每一条步骤原始数据
    for item in plan:
        try:
            # 如果本身已是PlanStep实例，直接加入临时列表
            if isinstance(item, PlanStep):
                cleaned.append(item)
                continue

            # 字典类型通过from_dict工厂方法转为PlanStep对象
            step = PlanStep.from_dict(item)
            cleaned.append(step)
        except Exception:
            # 单条数据解析异常（字段缺失、格式错误等），静默跳过该条，不中断整体校验
            continue

    # -------------------------- 步骤ID去重逻辑 --------------------------
    # 记录已出现过的step_id，用于去重判断
    seen_ids = set()
    unique_steps = []
    for step in cleaned:
        # 重复ID直接跳过，只保留第一次出现的步骤
        if step.id in seen_ids:
            continue
        seen_ids.add(step.id)
        unique_steps.append(step)

    # -------------------------- 依赖合法性校验逻辑 --------------------------
    # 收集所有合法步骤ID，作为依赖校验白名单
    valid_ids = {step.id for step in unique_steps}
    final_steps = []

    for step in unique_steps:
        # 规则1：禁止步骤依赖自身
        if step.id in step.depends_on:
            continue
        # 规则2：所有依赖ID必须存在于当前计划的合法步骤中，不存在则丢弃当前步骤
        if not all(dep_id in valid_ids for dep_id in step.depends_on):
            continue
        # 同时满足两条依赖规则的步骤加入最终结果
        final_steps.append(step)

    # 返回经过类型转换、去重、依赖合法性校验后的标准计划步骤列表
    return final_steps