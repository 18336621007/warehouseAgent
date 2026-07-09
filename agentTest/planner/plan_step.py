from agentTest.config.tools import TOOLS


class PlanStep:
    def __init__(self, id, name, tool, depends_on=None, inputs=None, meta=None):
        self.id = id
        self.name = name
        self.tool = tool
        self.depends_on = depends_on or []
        self.inputs = inputs or {}
        self.meta = meta or {}

    @classmethod
    def from_dict(cls, data: dict):
        if not isinstance(data, dict):
            raise ValueError("plan step must be dict")

        step_id = data.get("id")
        name = data.get("name") or data.get("step")
        tool = data.get("tool")
        inputs = data.get("inputs")
        depends_on = data.get("depends_on")
        meta = data.get("meta")

        # 校验必填字段
        if not isinstance(step_id, str) or not step_id.strip():
            raise ValueError("plan step missing id")

        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"plan step {step_id} missing name")

        if not isinstance(tool, str) or not tool.strip():
            raise ValueError(f"plan step {step_id} missing tool")

        # 校验工具名是否合法
        if tool not in TOOLS:
            raise ValueError(f"plan step {step_id} invalid tool: {tool}")

        # 校验字段类型
        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict):
            raise ValueError(f"plan step {step_id} inputs must be dict")

        if depends_on is None:
            depends_on = []
        if not isinstance(depends_on, list):
            raise ValueError(f"plan step {step_id} depends_on must be list")

        if meta is None:
            meta = {}
        if not isinstance(meta, dict):
            raise ValueError(f"plan step {step_id} meta must be dict")

        #校验tool参数是否合法
        required_fields = TOOLS[tool].get("args_schema", {}).get("required", [])
        for field in required_fields:
            if field not in inputs:
                raise ValueError(f"plan step {step_id} missing required input: {field}")

        return cls(
            id=step_id,
            name=name,
            tool=tool,
            depends_on=data.get("depends_on") or [],
            inputs=data.get("inputs") or {},
            meta=data.get("meta") or {},
        )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "tool": self.tool,
            "depends_on": self.depends_on,
            "inputs": self.inputs,
            "meta": self.meta,
        }
