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

        if not step_id:
            raise ValueError("plan step missing id")
        if not name:
            raise ValueError(f"plan step {step_id} missing name")

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
