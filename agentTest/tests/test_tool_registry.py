# 统一工具注册表单元测试
# 覆盖：register / get_by_name / 按组取工具 / 重复注册报错 / 安全元数据
import unittest

from agentTest.langgraph_app.tools.registry import ToolRegistry, ToolSpec, ToolSecurity
from agentTest.langgraph_app.tools.advisor_tools import build_advisor_tools


class _StubTool:
    """轻量工具替身：只需 name / description，模拟 LangChain 工具接口。"""

    def __init__(self, name, description="stub"):
        self.name = name
        self.description = description


class ToolRegistryTest(unittest.TestCase):
    """注册表核心行为：唯一性、按组取用、安全元数据。"""

    def test_register_and_get_by_name(self):
        reg = ToolRegistry()
        reg.register(ToolSpec(name="sql_query", description="run sql", tool=_StubTool("sql_query")))
        spec = reg.get_by_name("sql_query")
        self.assertEqual(spec.name, "sql_query")
        self.assertEqual(spec.tool.name, "sql_query")

    def test_get_by_group(self):
        reg = ToolRegistry()
        reg.register(ToolSpec(name="sql_query", description="run sql", tool=_StubTool("sql_query"), groups=("seeker",)))
        reg.register(ToolSpec(name="search_columns", description="search cols", tool=_StubTool("search_columns"), groups=("advisor",)))
        self.assertEqual([t.name for t in reg.get(group="advisor")], ["search_columns"])
        self.assertEqual([t.name for t in reg.get(group="seeker")], ["sql_query"])
        # group=None 返回全部
        self.assertCountEqual([t.name for t in reg.get()], ["sql_query", "search_columns"])

    def test_duplicate_register_rejected(self):
        reg = ToolRegistry()
        reg.register(ToolSpec(name="sql_query", description="run sql", tool=_StubTool("sql_query")))
        with self.assertRaises(ValueError):
            reg.register(ToolSpec(name="sql_query", description="dup", tool=_StubTool("sql_query")))

    def test_missing_name_raises(self):
        reg = ToolRegistry()
        with self.assertRaises(KeyError):
            reg.get_by_name("not_exists")

    def test_security_defaults(self):
        reg = ToolRegistry()
        reg.register(ToolSpec(name="t", description="d", tool=_StubTool("t")))
        sec = reg.get_by_name("t").security
        self.assertTrue(sec.read_only)
        self.assertEqual(sec.row_limit, 0)
        self.assertTrue(sec.whitelist_only)

    def test_advisor_tools_grouping(self):
        """Advisor 工具统一注册进 advisor 组，工具名与数量保持不变。"""
        reg = ToolRegistry()
        for t in build_advisor_tools():
            security = ToolSecurity(
                read_only=(t.name != "update_draft_plan"),
                row_limit=100 if t.name == "query_stored_result" else 0,
                whitelist_only=True,
            )
            reg.register(ToolSpec(name=t.name, description=t.description, tool=t, groups=("advisor",), security=security))
        names = [t.name for t in reg.get(group="advisor")]
        self.assertEqual(names, [
            "search_databases", "search_tables", "search_columns",
            "update_draft_plan", "query_stored_result",
        ])
        self.assertFalse(reg.get_by_name("update_draft_plan").security.read_only)
        self.assertEqual(reg.get_by_name("query_stored_result").security.row_limit, 100)


if __name__ == "__main__":
    unittest.main()
