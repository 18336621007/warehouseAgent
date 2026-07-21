# 最小语义层：业务问题到数据表与字段的结构化映射
# 每一条语义条目描述了某类业务问题应该优先查哪张表、用哪些字段、注意什么规则。
# 分区字段格式定义（唯一真实来源）
PARTITION_FIELD_FORMATS = {
    "pt_dt": "yyyyMMdd",   # 如 20260719
}

SEMANTIC_ENTRIES = [
    {
        # 订单量 / 订单状态类问题
        "keywords": ["订单量", "订单分布", "订单数", "租赁中", "租赁订单", "订单状态", "已完成", "已取消", "待支付", "待审核"],
        "default_table": "dwd_trip.dwd_exchange_order_rent_detail_hour",
        "key_fields": ["rent_status", "pt_dt", "pt_platform", "rent_days", "rent_rmb"],
        "default_time_field": "pt_dt",
        "notes": "需要按 rent_status 过滤状态。订单粒度：一条记录代表一个订单。",
    },
    {
        # 优惠 / 补贴 / 支付类问题
        "keywords": ["优惠", "补贴", "实际支付", "优惠金额", "优惠后价格", "抖音", "渠道", "渠道占比", "实付"],
        "default_table": "dwm_trip.dwm_exchange_order_addition_detail_hour",
        "key_fields": ["discounted_money", "discount_price", "real_pay_price", "market_name", "pt_dt", "pt_platform"],
        "default_time_field": "pt_dt",
        "notes": "market_name 用于区分官方渠道和抖音渠道。优惠金额为单笔发放金额。",
    },
    {
        # 日报 / 环比 / 同比 / 运营指标类问题
        "keywords": ["日报", "环比", "同比", "运营", "租赁中订单数", "实收租金", "运营指标", "运营报表"],
        "default_table": "ads_trip.ads_exchange_platform_operations_report_day",
        "key_fields": ["this_renting_order", "accrued_rent_received", "pt_dt"],
        "default_time_field": "pt_dt",
        "notes": "仅用于固化指标查询，不做灵活明细下钻。默认时间字段 pt_dt 表示日报日期。",
    },
]


# 根据用户问题匹配语义条目，返回相关语义指引
def match_semantic_entries(question: str):
    matched_entries = []

    for entry in SEMANTIC_ENTRIES:
        for keyword in entry.get("keywords", []):
            if keyword in question:
                matched_entries.append(entry)
                break

    return matched_entries


# 将匹配到的语义条目格式化为 Prompt 可用的文本
def format_semantic_context(matched_entries):
    if not matched_entries:
        return "无额外语义指引。"

    lines = []
    for idx, entry in enumerate(matched_entries):
        table = entry.get("default_table", "")
        fields = ", ".join(entry.get("key_fields", []))
        time_field = entry.get("default_time_field", "")
        notes = entry.get("notes", "")
        # 追加分区字段格式提示
        time_format = PARTITION_FIELD_FORMATS.get(time_field, "")
        format_hint = f"（格式: {time_format}）" if time_format else ""

        text = (
            f"语义指引 {idx + 1}:\n"
            f"- 推荐表: {table}\n"
            f"- 关键字段: {fields}\n"
            f"- 默认时间字段: {time_field}{format_hint}\n"
            f"- 注意事项: {notes}\n"
        )
        lines.append(text)

    return "\n".join(lines)


