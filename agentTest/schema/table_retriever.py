class TableRetriever:
    KEYWORD_MAP = {
        "订单": ["order", "orders"],
        "用户": ["user", "users"],
        "销售额": ["amount", "pay", "gmv", "sale"],
        "支付": ["pay"],
        "商品": ["product", "sku", "item"],
    }

    def retrieve(self, query: str, tables: list[dict], top_k: int = 5):
        """
        根据用户查询文本对数据表做关键词匹配打分检索，返回匹配度最高的top_k张表
        :param query: 用户输入的查询语句/自然语言问题
        :param tables: 全量表信息列表，每个元素包含 table_name、table_comment
        :param top_k: 最多返回匹配分数靠前的表数量，默认5条
        :return: 按匹配分数降序排序的表列表，每条包含表名、注释、匹配得分
                    "table_name":
                    "table_comment":
                    "score": 0,
        """
        # 处理空查询兜底
        query = query or ""
        # 存储有匹配分数的表
        scored_tables = []

        # 遍历所有数据表，逐个计算匹配分数
        for table in tables:
            table_name = table.get("table_name", "").lower()
            table_comment = table.get("table_comment", "")
            score = 0  # 当前表初始匹配分数为0

            # 遍历预设关键词映射配置 KEYWORD_MAP
            # KEYWORD_MAP 格式示例：{关键词: [别名1,别名2...]}
            for keyword, aliases in self.KEYWORD_MAP.items():
                # 如果用户查询包含当前关键词，才进行加分判断
                if keyword in query:
                    # 关键词出现在表注释中，权重+2（注释匹配优先级更高）
                    if keyword in table_comment:
                        score += 2
                    # 遍历该关键词所有别名，别名命中表名则权重+1
                    for alias in aliases:
                        if alias in table_name:
                            score += 1

            # 只要得分大于0，说明存在匹配，加入候选列表
            if score > 0:
                scored_tables.append({
                    "table_name": table.get("table_name"),
                    "table_comment": table.get("table_comment", ""),
                    "score": score,
                })

        # 场景1：没有任何表匹配到关键词，直接返回前top_k张原始表，分数置0
        if not scored_tables:
            return [
                {
                    "table_name": table.get("table_name"),
                    "table_comment": table.get("table_comment", ""),
                    "score": 0,
                }
                for table in tables[:top_k]
            ]

        # 场景2：存在匹配表，按匹配分数从高到低排序
        scored_tables.sort(key=lambda item: item["score"], reverse=True)
        # 截取前top_k个高分表返回
        return scored_tables[:top_k]