from agentTest.state.step_status import StepStatus


class Scheduler:

    def __init__(self):
        self.plan = {}

    def init(self, plan, state):
        self.plan = {step.id: step for step in plan}
        for step in plan:
            state.step_status[step.id] = StepStatus.PENDING

    def update_ready(self, state):
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
