from agentTest.validate_args import validate_args


class ToolRegistry(object):
    # ToolRegistry 是工具注册与执行路由中心：
    # - 统一保存工具名 -> 工具实例映射
    # - 在执行前根据 TOOLS schema 校验参数
    # - 调用具体工具的 run(args) 完成执行

    def __init__(self, tools_schema):
        # tools 保存实际工具实例，schema 保存工具参数约束定义
        self.tools = {}
        self.schema = tools_schema

    def register(self, name, tool):
        # 注册单个工具实例，供后续按工具名路由执行
        self.tools[name] = tool

    def execute(self, name, args):
        # 执行单个工具：
        # 1. 检查工具是否已注册
        # 2. 校验输入参数是否符合 schema
        # 3. 调用工具 run(args) 返回结果
        if name not in self.tools:
            raise Exception(f"工具未注册: {name}")

        schema = self.schema[name]["args_schema"]
        ok, msg = validate_args(args, schema)

        if not ok:
            raise Exception(f"参数错误: {msg}")

        return self.tools[name].run(args)
