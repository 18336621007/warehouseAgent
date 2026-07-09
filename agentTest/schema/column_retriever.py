class ColumnRetriever:
    """
    "name": column.get("name"),
    "type": column.get("type"),
    "comment": column.get("comment", ""),
    "score": 0,
    """
    KEYWORD_MAP = {
        "金额": ["amount", "amt", "pay_amt", "gmv"],
        "销售额": ["amount", "amt", "pay_amt", "gmv"],
        "用户": ["user", "user_id"],
        "城市": ["city"],
        "省份": ["province"],
        "地区": ["province", "city", "area", "region"],
        "时间": ["time", "date", "dt", "create_time", "pay_time"],
        "日期": ["date", "dt"],
        "订单": ["order", "order_id"],
    }

    def retrieve(self, query: str, table_schema: dict, top_k: int = 10):
        query = query or ""
        columns = table_schema.get("columns", [])
        scored_columns = []

        for column in columns:
            column_name = column.get("name", "").lower()
            column_comment = column.get("comment", "")
            score = 0

            for keyword, aliases in self.KEYWORD_MAP.items():
                if keyword in query:
                    if keyword in column_comment:
                        score += 2
                    for alias in aliases:
                        if alias in column_name:
                            score += 1

            if score > 0:
                scored_columns.append({
                    "name": column.get("name"),
                    "type": column.get("type"),
                    "comment": column.get("comment", ""),
                    "score": score,
                })

        if not scored_columns:
            return [
                {
                    "name": column.get("name"),
                    "type": column.get("type"),
                    "comment": column.get("comment", ""),
                    "score": 0,
                }
                for column in columns[:top_k]
            ]

        scored_columns.sort(key=lambda item: item["score"], reverse=True)
        return scored_columns[:top_k]
