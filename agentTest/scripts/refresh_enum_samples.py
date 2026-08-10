# -*- coding: utf-8 -*-
"""补采 enriched_columns 中 sample_values 为空的字段枚举值（只读 Hive，仅更新 MySQL 采样列）。

用法（python 命令）：
  python agentTest/scripts/refresh_enum_samples.py
  python agentTest/scripts/refresh_enum_samples.py --table ads_exchange_platform_operations_report_day
  python agentTest/scripts/refresh_enum_samples.py --column company_category
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
from agentTest.metadata.mysql_store import _get_connection, init_metadata_tables, save_column

SAMPLE_LIMIT = 100  # 枚举采样上限


def _load_empty_sample_columns(table: str = "", column: str = "") -> list[dict]:
    """读取 enriched_columns 中 sample_values 为空（含 NULL/[]）的字段记录。"""
    sql = (
        "SELECT full_key, database_name, table_name, column_name, "
        "fields_type, relations, field_aliases, original_comment, meta_source "
        "FROM enriched_columns "
        "WHERE (sample_values IS NULL OR JSON_LENGTH(sample_values) = 0)"
    )
    params = []
    if table:
        sql += " AND table_name = %s"
        params.append(table)
    if column:
        sql += " AND column_name = %s"
        params.append(column)
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, tuple(params))
            keys = ["full_key", "database_name", "table_name", "column_name",
                    "fields_type", "relations", "field_aliases", "original_comment", "meta_source"]
            return [dict(zip(keys, row)) for row in cursor.fetchall()]
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


def main() -> int:
    parser = argparse.ArgumentParser(description="补采 enriched_columns 中空枚举字段的采样值")
    parser.add_argument("--table", default="", help="只补采指定表名")
    parser.add_argument("--column", default="", help="只补采指定字段名")
    parser.add_argument("--dry-run", action="store_true", help="仅打印待补采清单，不写库")
    args = parser.parse_args()

    # ?? enriched_columns ??????????????? meta_source ??
    init_metadata_tables()

    columns = _load_empty_sample_columns(table=args.table, column=args.column)
    if not columns:
        print("没有需要补采的空枚举字段。")
        return 0
    print(f"待补采字段数: {len(columns)}")
    if args.dry_run:
        for col in columns:
            print(f"  - {col['full_key']}")
        return 0

    datasource = HiveDataSource()
    updated, empty = 0, []
    for col in columns:
        samples = _sample_enum_values(
            datasource, col["database_name"], col["table_name"], col["column_name"]
        )
        if not samples:
            empty.append(col["full_key"])
            print(f"  跳过（Hive 无数据或查询失败）: {col['full_key']}")
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
        updated += 1
        print(f"  更新 {col['full_key']}: {samples}")
    print(f"完成：更新 {updated} 个字段，跳过 {len(empty)} 个。")
    if empty:
        print("跳过清单（无数据/查询失败）:")
        for full_key in empty:
            print(f"  - {full_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
