# -*- coding: utf-8 -*-
"""枚举采样：补采空 sample_values 或 --refresh 强制刷新指定表/字段。

只读 Hive 采样，内容变化才写库 + 记录 enum_refreshed 事件 + 同步 column 向量层。

用法（python 命令）：
  python agentTest/scripts/refresh_enum_samples.py
  python agentTest/scripts/refresh_enum_samples.py --table ads_exchange_platform_operations_report_day
  python agentTest/scripts/refresh_enum_samples.py --column company_category --refresh
  python agentTest/scripts/refresh_enum_samples.py --refresh --skip-vector
  python agentTest/scripts/refresh_enum_samples.py --dry-run
"""
import argparse
import json
import pathlib
import sys

# ???????? sys.path??????????????
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ?????? .env?????????????????/Hive ??
import dotenv
dotenv.load_dotenv(_PROJECT_ROOT / "agentTest" / ".env")

from agentTest.datasource.hive_datasource import HiveDataSource
from agentTest.metadata.mysql_store import (
    _get_connection, init_metadata_tables, save_column,
    log_metadata_changes, EVENT_ENUM_REFRESHED,
)
from agentTest.db.metadata_scope import is_allowed_table

SAMPLE_LIMIT = 100  # 枚举采样上限


def _load_columns(only_empty: bool = True, table: str = "", column: str = "") -> list[dict]:
    """读取 enriched_columns 字段记录；only_empty=True 时仅取 sample_values 为空（含 NULL/[]）"""
    sql = (
        "SELECT full_key, database_name, table_name, column_name, "
        "fields_type, relations, field_aliases, sample_values, original_comment, meta_source "
        "FROM enriched_columns"
    )
    params = []
    conditions = []
    if only_empty:
        conditions.append("(sample_values IS NULL OR JSON_LENGTH(sample_values) = 0)")
    if table:
        conditions.append("table_name = %s")
        params.append(table)
    if column:
        conditions.append("column_name = %s")
        params.append(column)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, tuple(params))
            keys = ["full_key", "database_name", "table_name", "column_name",
                    "fields_type", "relations", "field_aliases", "sample_values",
                    "original_comment", "meta_source"]
            result = []
            for row in cursor.fetchall():
                item = dict(zip(keys, row))
                item["sample_values"] = json.loads(item["sample_values"]) if item["sample_values"] else []
                # 白名单过滤：跳过已移出接入范围的表
                if not is_allowed_table(item["table_name"], item["database_name"]):
                    continue
                result.append(item)
            return result
    finally:
        conn.close()


def _sample_enum_values(datasource, database_name: str, table_name: str, column_name: str) -> list[str]:
    """Hive 采样字段去重非空枚举值。"""
    sql = (
        f"SELECT DISTINCT {column_name} "
        f"FROM {database_name}.{table_name} "
        f"WHERE {column_name} IS NOT NULL AND {column_name} != '' "
        f"LIMIT {SAMPLE_LIMIT}"
    )
    try:
        result = datasource.query(sql)
        return [str(row[0]) for row in result["rows"]]
    except Exception:
        return []


def _sync_column_vector_index():
    """将 MySQL 最新字段增强文档同步到 column 层向量索引（M3 upsert/delete），失败不阻断"""
    try:
        from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
        from agentTest.langchain_app.documents.enriched_column_documents import EnrichedColumnDocumentsBuilder
        from agentTest.langchain_app.vectorstores.schema_vector_store import SchemaVectorStore
        from agentTest.langchain_app.app_builder import _CACHE_COLUMN_DIR

        documents = EnrichedColumnDocumentsBuilder().build_documents()
        manager = SchemaVectorStore(BailianEmbeddings())
        _vector_store, sync_stats = manager.sync_documents(
            _CACHE_COLUMN_DIR, documents, return_stats=True
        )
        if sync_stats.get("rebuilt"):
            print(f"  向量索引已同步（column 层，全量重建 {len(documents)} 条）")
        else:
            added = sync_stats.get("added", 0)
            changed = sync_stats.get("changed", 0)
            removed = sync_stats.get("removed", 0)
            if added or changed or removed:
                print(f"  向量索引已同步（column 层，新增 {added} / 更新 {changed} / 删除 {removed}）")
            else:
                print("  向量索引已同步（column 层，无变化）")
    except Exception as e:
        print(f"  警告: column 向量索引同步失败（不影响 MySQL 更新）: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="补采/刷新 enriched_columns 字段枚举采样值")
    parser.add_argument("--table", default="", help="只处理指定表名")
    parser.add_argument("--column", default="", help="只处理指定字段名")
    parser.add_argument("--refresh", action="store_true", help="强制重新采样（默认只补采空 sample_values）")
    parser.add_argument("--skip-vector", action="store_true", help="不同步 column 向量索引")
    parser.add_argument("--dry-run", action="store_true", help="仅打印待处理清单，不写库")
    args = parser.parse_args()

    init_metadata_tables()

    columns = _load_columns(only_empty=not args.refresh, table=args.table, column=args.column)
    if not columns:
        print("没有需要处理字段。")
        return 0
    print(f"待处理字段数: {len(columns)}")
    if args.dry_run:
        for col in columns:
            print(f"  - {col['full_key']}")
        return 0

    datasource = HiveDataSource()
    updated, empty, unchanged = 0, [], 0
    for col in columns:
        samples = _sample_enum_values(
            datasource, col["database_name"], col["table_name"], col["column_name"]
        )
        if not samples:
            empty.append(col["full_key"])
            print(f"  跳过（Hive 无数据或查询失败）: {col['full_key']}")
            continue

        # 内容变化才写库（--refresh 模式下无变化也跳过，避免无意义更新与向量重建）
        if samples == col["sample_values"]:
            unchanged += 1
            print(f"  无变化，跳过: {col['full_key']}")
            continue

        data = {
            "domain": "",
            "fields_type": col["fields_type"],
            "relations": json.loads(col["relations"] or "[]"),
            "field_aliases": json.loads(col["field_aliases"] or "[]"),
            "_original_comment": col["original_comment"] or "",
            "meta_source": col["meta_source"] or "ddl_comment",
        }
        save_column(
            col["full_key"], col["database_name"], col["table_name"],
            col["column_name"], data, samples,
        )
        log_metadata_changes([{
            "event_type": EVENT_ENUM_REFRESHED,
            "database_name": col["database_name"],
            "table_name": col["table_name"],
            "column_name": col["column_name"],
            "detail": {"full_key": col["full_key"], "sample_count": len(samples)},
        }])
        updated += 1
        print(f"  更新 {col['full_key']}: {samples}")

    print(f"完成：更新 {updated} 个字段，跳过 {unchanged} 个无变化，跳过 {len(empty)} 个无数据/失败。")
    if empty:
        print("跳过清单（无数据/查询失败）:")
        for full_key in empty:
            print(f"  - {full_key}")

    # M3: 内容变化后同步 column 向量层
    if updated and not args.skip_vector:
        _sync_column_vector_index()
    return 0


