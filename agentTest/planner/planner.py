from agentTest.planner.plan_step import PlanStep


class RulePlanner:

    def plan(self, query: str):

        if "订单" in query and "金额" not in query:
            return [
                PlanStep(
                    id="s1",
                    name="query_orders",
                    tool="mysql_query",
                    depends_on=[],
                    inputs={
                        "sql": "SELECT * FROM orders",
                    },
                )
            ]

        if "金额" in query:
            return [
                PlanStep(
                    id="s1",
                    name="query_orders",
                    tool="mysql_query",
                    depends_on=[],
                    inputs={
                        "sql": "SELECT * FROM orders",
                    },
                ),
                PlanStep(
                    id="s2",
                    name="query_users",
                    tool="mysql_query",
                    depends_on=[],
                    inputs={
                        "sql": "SELECT * FROM users",
                    },
                ),
                PlanStep(
                    id="s3",
                    name="aggregate",
                    tool="python_tool",
                    depends_on=["s1", "s2"],
                    inputs={
                        "orders": "s1",
                        "users": "s2"
                    }
                )
            ]

        return [
            PlanStep(
                id="s1",
                name="general_query",
                tool=None,
                depends_on=[],
                inputs={}
            )
        ]
