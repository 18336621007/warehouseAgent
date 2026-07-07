from agentTest.state.step_status import StepStatus


class Scheduler:

    TERMINAL_STATUSES = {
        StepStatus.SUCCESS,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
    }

    def __init__(self):
        self.plan = {}

    def init(self, plan, state):
        #初始化调度器
        self.plan = {step.id: step for step in plan}
        for step in plan:
            state.step_status[step.id] = StepStatus.PENDING

    def is_terminal(self, status):
        """判断是否可以结束该step了，成功/失败/跳过都可以结束"""
        return status in self.TERMINAL_STATUSES

    def get_unfinished_steps(self, state):
        """得到还不能结束的step"""
        return [
            step_id
            for step_id in self.plan
            if not self.is_terminal(state.step_status.get(step_id))
        ]

    def all_steps_finished(self, state):
        """判断是否都完成了"""
        return len(self.get_unfinished_steps(state)) == 0

    def update_ready(self, state):
        for step_id, step in self.plan.items():
            status = state.step_status[step_id]
            """
                Scheduler
                负责的状态变化只有：
                PENDING -> READY
                RETRY -> READY
                READY -> RUNNING
            """
            # 如果不是pending和retry，不更新状态
            if status not in [StepStatus.PENDING, StepStatus.RETRY]:
                continue

            # 若所有上游依赖已完成，更新该step状态未ready
            if all(
                state.step_status.get(dep) == StepStatus.SUCCESS
                for dep in step.depends_on
            ):
                state.step_status[step_id] = StepStatus.READY

    def get_ready_batch(self, state):
        """获取当前就绪的步骤批次"""
        self.update_ready(state)

        ready = []
        for step_id, step in self.plan.items():
            if state.step_status[step_id] == StepStatus.READY:
                ready.append(step)
                state.step_status[step_id] = StepStatus.RUNNING

        return ready

    def mark_failed_propagation(self, failed_step_id, state):
        for step_id, step in self.plan.items():
            if failed_step_id in step.depends_on:
                state.step_status[step_id] = StepStatus.SKIPPED
                self.mark_failed_propagation(step_id, state)
