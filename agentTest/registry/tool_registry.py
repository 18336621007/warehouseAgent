from utils import Utils
class ToolRegistry(object):
    def __init__(self, tools_schema):
        self.tools = {}
        self.schema = tools_schema

    def register(self, name, tool):
        self.tools[name] = tool

    def execute(self, name, args):
        if name not in self.tools:
            raise Exception(f"工具未注册: {name}")

        #加工具参数校验
        schema  = self.schema[name]["args_schema"]
        ok, msg = Utils.validate_args(args, schema)

        if not ok:
            raise Exception(f"参数错误:{msg}")

        return self.tools[name].run(args)


