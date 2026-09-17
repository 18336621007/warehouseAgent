# 数据源注册表：按引擎注册查询执行器，按逻辑表(schema.table)解析引擎候选链
# 引擎路由配置见 agentTest/config/datasource_scope.yaml（单一事实源）
import os

import yaml

from agentTest.datasource.base_datasource import BaseDataSource

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "datasource_scope.yaml"
)

# 默认引擎候选：配置缺失/损坏时回退 Trino 优先、Hive 兜底（与线上一致）
_DEFAULT_SCOPE = {
    "default": ["trino", "hive"],
    "databases": {},
    "tables": {},
}

_scope_cache = None
_scope_mtime = None


def load_engine_scope(force_reload: bool = False) -> dict:
    """加载引擎路由配置；配置 mtime 变化时自动刷新，缺失/损坏时回退默认。"""
    global _scope_cache, _scope_mtime
    # 配置文件 mtime 变化时强制刷新（引擎映射变更无需重启即可生效）
    if not force_reload and _scope_cache is not None:
        try:
            current_mtime = os.path.getmtime(_CONFIG_PATH)
        except OSError:
            current_mtime = None
        if current_mtime != _scope_mtime:
            force_reload = True
    if _scope_cache is not None and not force_reload:
        return _scope_cache

    scope = {"default": list(_DEFAULT_SCOPE["default"]), "databases": {}, "tables": {}}
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fp:
            data = yaml.safe_load(fp) or {}
        raw = data.get("engine_scope") or {}
        if raw.get("default"):
            scope["default"] = [str(e).lower() for e in raw["default"]]
        scope["databases"] = {
            str(db).lower(): [str(e).lower() for e in cands]
            for db, cands in (raw.get("databases") or {}).items()
        }
        scope["tables"] = {
            str(t).lower(): [str(e).lower() for e in cands]
            for t, cands in (raw.get("tables") or {}).items()
        }
    except Exception:
        # 配置缺失/损坏时保持默认，不影响查询可用性
        pass
    _scope_cache = scope
    try:
        _scope_mtime = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        _scope_mtime = None
    return _scope_cache


def resolve_engine_candidates(table_identifier: str) -> list[str]:
    """按逻辑表(schema.table)解析引擎候选链（有序，第一位首选）。
    优先级：tables 精确匹配 > databases 库匹配 > default。
    """
    identifier = str(table_identifier or "").strip().lower()
    schema = identifier.split(".", 1)[0] if "." in identifier else ""
    scope = load_engine_scope()
    tables = scope.get("tables") or {}
    if identifier in tables:
        return list(tables[identifier])
    databases = scope.get("databases") or {}
    if schema in databases:
        return list(databases[schema])
    return list(scope.get("default") or [])


class DataSourceRegistry:
    # 引擎执行器注册表：register 后按引擎名取数据源实例
    def __init__(self):
        self._engines = {}

    def register(self, engine: str, datasource: BaseDataSource) -> None:
        # 注册引擎执行器（engine 需与 datasource.engine 一致）
        self._engines[str(engine).lower()] = datasource

    def get_datasource(self, engine: str):
        # 按引擎名取数据源实例，未注册返回 None
        return self._engines.get(str(engine).lower())

    def get_candidates(self, table_identifier: str) -> list[str]:
        # 表 -> 引擎候选链（仅返回已注册的引擎，未注册引擎自动跳过）
        return [
            e for e in resolve_engine_candidates(table_identifier)
            if e in self._engines
        ]

    def engines(self) -> list[str]:
        # 返回已注册引擎名列表
        return list(self._engines.keys())
