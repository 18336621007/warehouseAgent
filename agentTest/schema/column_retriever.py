class ColumnRetriever:
    # 字段级关键词映射，用于从候选表中缩小到更可能相关的字段
    KEYWORD_MAP = {
        "金额": ["amount", "amt", "pay_amt", "gmv", "actual_amt", "order_amt"],
        "销售额": ["amount", "amt", "pay_amt", "gmv", "actual_amt", "order_amt"],
        "用户": ["user", "user_id", "buyer_id"],
        "城市": ["city", "city_name"],
        "省份": ["province", "province_name"],
        "地区": ["province", "city", "area", "region"],
        "时间": ["time", "date", "dt", "create_time", "pay_time", "order_time"],
        "日期": ["date", "dt", "biz_date"],
        "订单": ["order", "order_id"],
        "状态": ["status", "state", "order_status"],
    }

    def retrieve(self, query: str, table_schema: dict, top_k: int = 10):
        query = (query or "").strip().lower()
        columns = table_schema.get("columns", [])
        scored_columns = []

        for column in columns:
            column_name = column.get("name", "")
            column_type = column.get("type", "")
            column_comment = column.get("comment", "")

            column_name_lower = column_name.lower()
            column_comment_lower = column_comment.lower()
            score = 0

            for keyword, aliases in self.KEYWORD_MAP.items():
                if keyword not in query:
                    continue

                # 字段名 alias 命中优先级最高
                for alias in aliases:
                    if alias in column_name_lower:
                        score += 3

                # 字段注释命中作为补充分
                if keyword in column_comment_lower:
                    score += 2

                # 关键词和字段名直接相关时给基础分
                if keyword in column_name_lower:
                    score += 1

            if score > 0:
                scored_columns.append({
                    "name": column_name,
                    "type": column_type,
                    "comment": column_comment,
                    "nullable": column.get("nullable"),
                    "partition_key": column.get("partition_key", False),
                    "score": score,
                })

        # 没有明显命中时，只回退少量字段，避免真实 Hive 场景噪音过大
        if not scored_columns:
            fallback_columns = []
            for column in columns[: min(top_k, 3)]:
                fallback_columns.append({
                    "name": column.get("name", ""),
                    "type": column.get("type", ""),
                    "comment": column.get("comment", ""),
                    "nullable": column.get("nullable"),
                    "partition_key": column.get("partition_key", False),
                    "score": 0,
                })
            return fallback_columns

        scored_columns.sort(key=lambda item: (-item["score"], item["name"]))
        return scored_columns[:top_k]
