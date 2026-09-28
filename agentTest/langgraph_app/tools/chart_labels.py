# coding: utf-8
# chart_labels - field to chinese label map
import re
FIELD_LABELS = {
    "platform": "\u5e73\u53f0",
    "pt_platform": "\u5e73\u53f0",
    "company_name": "\u7ecf\u9500\u5546",
    "region_name": "\u5927\u533a",
    "region_manager_name": "\u5927\u533a\u7ecf\u7406",
    "pt_dt": "\u65e5\u671f",
    "date_day": "\u65e5\u671f",
    "addition_order_num": "\u65b0\u589e\u8ba2\u5355\u6570",
    "return_order_num": "\u9000\u79df\u8ba2\u5355\u6570",
    "return_count": "\u9000\u79df\u8ba2\u5355\u6570",
    "net_addition_order_num": "\u51c0\u589e\u8ba2\u5355\u6570",
    "renting_order_num": "\u79df\u8d41\u4e2d\u8ba2\u5355\u6570",
    "month_rent_order_num": "\u6708\u79df\u8ba2\u5355\u6570",
    "overdue_order_num_30": "\u903e\u671f\u5927\u4e8e30\u5929\u8ba2\u5355\u6570",
    "overdue_order_num": "\u903e\u671f\u8ba2\u5355\u6570",
    "battery_inventory_num": "\u7535\u6c60\u5e93\u5b58\u6570",
    "battery_stock_num": "\u7535\u6c60\u5e93\u5b58\u6570",
    "cabinet_activation_count": "\u6fc0\u6d3b\u7535\u67dc\u6570",
    "activation_count": "\u6fc0\u6d3b\u7535\u67dc\u6570",
}
_WORD = {"addition":"\u65b0\u589e","return":"\u9000\u79df","net":"\u51c0\u589e","order":"\u8ba2\u5355","num":"\u6570","count":"\u6570\u91cf","battery":"\u7535\u6c60","cabinet":"\u7535\u67dc","platform":"\u5e73\u53f0","region":"\u5927\u533a","date":"\u65e5\u671f","day":"\u65e5","month":"\u6708\u79df","overdue":"\u903e\u671f"}
def field_label(field):
    key = str(field or "").strip()
    if key in FIELD_LABELS: return FIELD_LABELS[key]
    out = []
    for part in re.split(r"[_.\-/]", key):
        if part in _WORD: out.append(_WORD[part])
        else: out.append(part)
    label = "".join(out)
    return label if any(ord(c) > 127 for c in label) else key
