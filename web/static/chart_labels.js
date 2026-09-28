var CHART_FIELD_LABELS = {
    "platform": "平台",
    "pt_platform": "平台",
    "company_name": "经销商",
    "region_name": "大区",
    "region_manager_name": "大区经理",
    "pt_dt": "日期",
    "date_day": "日期",
    "addition_order_num": "新增订单数",
    "return_order_num": "退租订单数",
    "return_count": "退租订单数",
    "net_addition_order_num": "净增订单数",
    "renting_order_num": "租赁中订单数",
    "month_rent_order_num": "月租订单数",
    "overdue_order_num_30": "逾期大于30天订单数",
    "overdue_order_num": "逾期订单数",
    "battery_inventory_num": "电池库存数",
    "battery_stock_num": "电池库存数",
    "cabinet_activation_count": "激活电柜数",
    "activation_count": "激活电柜数"
};
var CHART_FIELD_WORDS = {
    "addition": "新增", "return": "退租", "net": "净增", "order": "订单",
    "num": "数", "count": "数量", "battery": "电池", "cabinet": "电柜",
    "platform": "平台", "region": "大区", "date": "日期", "day": "日",
    "month": "月租", "overdue": "逾期"
};
function chartFieldLabel(field) {
    if (field == null) return "";
    var key = String(field).trim();
    if (key in CHART_FIELD_LABELS) return CHART_FIELD_LABELS[key];
    var out = "";
    String(key).split(/[_.\-\/]/).forEach(function (part) {
        out += (part in CHART_FIELD_WORDS) ? CHART_FIELD_WORDS[part] : part;
    });
    return out || key;
}
