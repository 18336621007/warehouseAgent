class TableRetriever:
    # 表级关键词映射，用于基于中文问题召回更可能相关的表
    KEYWORD_MAP = {
        "订单": ["order", "orders"],
        "用户": ["user", "users"],
        "销售额": ["amount", "pay", "gmv", "sale"],
        "金额": ["amount", "amt", "pay_amt", "gmv"],
        "支付": ["pay", "payment"],
        "商品": ["product", "sku", "item"],
        "城市": ["city"],
        "时间": ["time", "date", "dt"],
        "状态": ["status", "state"],
    }

    def retrieve(self, query: str, tables: list[dict], top_k: int = 5):
        """
        根据用户查询文本对数据表做关键词匹配打分检索，返回匹配度最高的 top_k 张表。
        """
        query = (query or "").strip().lower()
        scored_tables = []

        for table in tables:
            database_name = table.get("database_name", "")
            table_name = table.get("table_name", "")
            table_comment = table.get("table_comment", "")

            table_name_lower = str(table_name or "").lower()
            table_comment_lower = str(table_comment or "").lower()
            score = 0

            for keyword, aliases in self.KEYWORD_MAP.items():
                if keyword not in query:
                    continue

                # 表名命中优先级更高，适合 metadata 场景
                for alias in aliases:
                    if alias in table_name_lower:
                        score += 3

                # 表注释命中作为辅助加分
                if keyword in table_comment_lower:
                    score += 2

                # 如果关键词本身和表名片段接近，也给基础分
                if keyword in table_name_lower:
                    score += 1

            if score > 0:
                scored_tables.append({
                    "database_name": database_name,
                    "table_name": table_name,
                    "table_comment": table_comment,
                    "table_type": table.get("table_type", ""),
                    "score": score,
                })

        # 没有明显命中时，只返回极少量候选，避免噪音过大
        if not scored_tables:
            fallback_tables = []
            for table in tables[: min(top_k, 2)]:
                fallback_tables.append({
                    "database_name": table.get("database_name", ""),
                    "table_name": table.get("table_name", ""),
                    "table_comment": table.get("table_comment", ""),
                    "table_type": table.get("table_type", ""),
                    "score": 0,
                })
            return fallback_tables

        scored_tables.sort(key=lambda item: (-item["score"], item["table_name"]))
        return scored_tables[:top_k]
