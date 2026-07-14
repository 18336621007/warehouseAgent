from agentTest.state.step_status import StepStatus


class Scheduler:
    # Scheduler 负责步骤状态流转和依赖调度，不负责真正执行工具

    TERMINAL_STATUSES = {
        StepStatus.SUCCESS,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
    }

    def __init__(self):
        # plan 保存当前轮次可调度的 step_id -> step 映射
        self.plan = {}

    def init(self, plan, state):
        # 初始化调度器：
        # - 建立 step_id 到 step 的映射
        # - 将所有 step 初始状态设置为 PENDING
        self.plan = {step.id: step for step in plan}
        for step in plan:
            state.step_status[step.id] = StepStatus.PENDING

    def is_terminal(self, status):
        # 成功 / 失败 / 跳过 都视为该 step 已终止
        return status in self.TERMINAL_STATUSES

    def get_unfinished_steps(self, state):
        # 返回当前仍未进入终态的 step_id 列表
        return [
            step_id
            for step_id in self.plan
            if not self.is_terminal(state.step_status.get(step_id))
        ]

    def all_steps_finished(self, state):
        # 判断本轮计划是否已全部执行结束
        return len(self.get_unfinished_steps(state)) == 0

    def update_ready(self, state):
        # 根据依赖完成情况更新状态流转：
        # - 只处理 PENDING / RETRY 状态的步骤
        # - 当所有上游依赖成功时，转成 READY
        for step_id, step in self.plan.items():
            status = state.step_status[step_id]
            if status not in [StepStatus.PENDING, StepStatus.RETRY]:
                continue

            if all(
                state.step_status.get(dep) == StepStatus.SUCCESS
                for dep in step.depends_on
            ):
                state.step_status[step_id] = StepStatus.READY

    def get_ready_batch(self, state):
        # 获取当前可执行的 ready step 批次，并将这些 step 标记为 RUNNING
        self.update_ready(state)

        ready = []
        for step_id, step in self.plan.items():
            if state.step_status[step_id] == StepStatus.READY:
                ready.append(step)
                state.step_status[step_id] = StepStatus.RUNNING

        return ready

    def mark_failed_propagation(self, failed_step_id, state):
        # 当某个 step 失败时，递归将所有依赖它的下游 step 标记为 SKIPPED
        for step_id, step in self.plan.items():
            if failed_step_id in step.depends_on:
                state.step_status[step_id] = StepStatus.SKIPPED
                self.mark_failed_propagation(step_id, state)
