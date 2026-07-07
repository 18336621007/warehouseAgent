from agentTest.planner.plan_step import PlanStep


def validate_plan(plan):
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
