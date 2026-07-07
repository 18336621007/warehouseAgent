class PythonTool:
    def run(self, args):
        data = args["data"]

        orders = data.get("orders", [])
        users = data.get("users", [])

        return {
            "order_count": len(orders),
            "user_count": len(users)
        }