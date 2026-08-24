# 元数据接入范围：从 agentTest/config/metadata.yaml 加载库级白名单与表级 include/exclude
# 提供统一判定入口，供元数据采集层与 SQL 门禁层共用（单一事实源）
import os
import re
import yaml

# 配置文件位于 agentTest/config/metadata.yaml
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "metadata.yaml")

# 兼容默认值：yaml 缺失或字段缺省时回退，保证安全边界不因配置错误失效
_DEFAULT_DATABASES = ["dwd_trip", "dwm_trip", "ads_trip", "dim_trip"]
_DEFAULT_TABLES = [
    "dwd_exchange_order_rent_detail_hour",
    "dwm_exchange_order_addition_detail_hour",
    "ads_exchange_platform_operations_report_day",
    "dim_company_snapshot_day",
]

_scope_cache = None
# 配置文件最后修改时间，变化时自动刷新缓存，感知运行期白名单变更
_scope_mtime = None


def load_metadata_scope(force_reload: bool = False) -> dict:
    """加载接入范围配置；配置缺失或解析失败时回退默认白名单
    配置文件 mtime 变化时自动 force_reload，避免服务运行期间白名单变更不生效"""
    global _scope_cache, _scope_mtime
    # 配置文件 mtime 变化时强制刷新（白名单变更无需重启即可生效）
    if not force_reload and _scope_cache is not None:
        try:
            current_mtime = os.path.getmtime(_CONFIG_PATH)
        except OSError:
            current_mtime = None
        if current_mtime != _scope_mtime:
            force_reload = True
    if _scope_cache is not None and not force_reload:
        return _scope_cache

    scope = {
        "databases": list(_DEFAULT_DATABASES),
        "include_tables": list(_DEFAULT_TABLES),
        "include_patterns": [],
        "exclude_tables": [],
        "auto_retire_missing": False,
    }
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fp:
            data = yaml.safe_load(fp) or {}
        raw = data.get("metadata_scope") or {}
        if raw.get("databases"):
            scope["databases"] = [str(x).lower() for x in raw["databases"]]
        # include_tables 缺省时回退默认 5 表；显式为空列表才表示"库下全部"
        if "include_tables" in raw:
            scope["include_tables"] = [str(x).lower() for x in (raw["include_tables"] or [])]
        if raw.get("include_patterns"):
            scope["include_patterns"] = [str(x) for x in raw["include_patterns"]]
        if raw.get("exclude_tables"):
            scope["exclude_tables"] = [str(x).lower() for x in raw["exclude_tables"]]
        scope["auto_retire_missing"] = bool(raw.get("auto_retire_missing", False))
    except Exception:
        # 配置缺失/损坏时保持默认，安全边界不因配置错误而失效
        pass
    _scope_cache = scope
    # 记录配置 mtime，供下次调用判断是否变化
    try:
        _scope_mtime = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        _scope_mtime = None
    return _scope_cache


def get_allowed_databases() -> list[str]:
    """返回库级白名单"""
    return list(load_metadata_scope()["databases"])


def get_include_tables() -> list[str]:
    """返回表级 include 条目（db.table 或裸表名）"""
    return list(load_metadata_scope()["include_tables"])


def get_include_patterns() -> list[str]:
    """返回表名正则（仅 include_tables 为空时生效）"""
    return list(load_metadata_scope()["include_patterns"])


def get_excluded_tables() -> list[str]:
    """返回排除表清单（废弃/内部/临时表）"""
    return list(load_metadata_scope()["exclude_tables"])


def get_auto_retire_missing() -> bool:
    """返回是否自动标记 Hive 中已消失的表为 retired"""
    return bool(load_metadata_scope()["auto_retire_missing"])


def _split_table_entries(entries) -> tuple[set, set]:
    """拆分 include/exclude 条目：带库名（db.table）与裸表名"""
    qualified = set()
    bare = set()
    for item in entries or []:
        if "." in item:
            qualified.add(item)
        else:
            bare.add(item)
    return qualified, bare


def is_allowed_table(table_name: str, database_name: str = "", table_name_occurrences: dict | None = None) -> bool:
    """统一接入范围判定：库级白名单 + include/exclude 表 + 可选正则。
    - db.table 条目精确匹配指定库表，优先级最高；
    - 裸表名条目仅在该表名于白名单库中唯一时生效（避免跨库同名表全部被加入）；
    - table_name_occurrences: {表名: 出现库数}，由 list_tables 提供用于唯一性判断。"""
    scope = load_metadata_scope()
    db = (database_name or "").strip().lower()
    table = (table_name or "").strip().lower()
    if not table:
        return False
    if db and db not in scope["databases"]:
        return False

    # 排除表优先（db.table 精确；裸表名唯一性约束）
    excl_qualified, excl_bare = _split_table_entries(scope["exclude_tables"])
    if db and f"{db}.{table}" in excl_qualified:
        return False
    if table in excl_bare:
        if table_name_occurrences is None or table_name_occurrences.get(table, 1) <= 1:
            return False

    # include 精确条目优先
    include_qualified, include_bare = _split_table_entries(scope["include_tables"])
    if db and f"{db}.{table}" in include_qualified:
        return True
    if table in include_bare:
        # 裸表名：同名表跨库时存在歧义，不允许加入
        if table_name_occurrences is None or table_name_occurrences.get(table, 1) <= 1:
            return True
        return False

    if include_qualified or include_bare:
        # include 有明确条目但未命中：不允许（避免 db.table 配置时无库名误放行）
        return False

    patterns = scope["include_patterns"]
    if patterns:
        return any(re.search(pattern, table) for pattern in patterns)
    return True  # include 为空且无正则：库下全部