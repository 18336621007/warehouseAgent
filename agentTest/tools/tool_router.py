# STEP_TO_TOOL_RULES = {
#
#     "query": "mysql_query",
#     "orders": "mysql_query",
#     "users": "mysql_query",
#     "payments": "mysql_query",
#
#     "aggregate": "python_tool",
#     "join": "python_tool",
# }
#
# class ToolRouter:
#     def __init__(self):
#         pass
#
#     def route(self, step_name):
#         for key, tool in STEP_TO_TOOL_RULES.items():
#             if key in step_name:
#                 return tool
#         return "mysql_query" # 兜底