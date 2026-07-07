class XComRecord:
    def __init__(self, step_id, tool, input_args, output, status):
        self.step_id = step_id
        self.tool = tool
        self.input_args = input_args
        self.output = output
        self.status = status #success/failed

    def to_dict(self):
        return {
            "step_id": self.step_id,
            "tool": self.tool,
            "input_args": self.input_args,
            "output": self.output,
            "status": self.status
        }