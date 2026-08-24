# -*- coding: utf-8 -*-
"""按当前白名单清理优秀案例残留（多退）：
- MySQL evaluated_dialogues：tables_used 中存在白名单外表的记录删除
- example_faiss_index：tables 中存在白名单外表的示例文档删除并落盘
- FAISS 与 MySQL 各自独立判定，避免一边残留导致运行时命中过期示例

用法（python 命令）：
  python -m agentTest.scripts.prune_examples_by_scope              # 实际清理
  python -m agentTest.scripts.prune_examples_by_scope --dry-run     # 只打印待清理清单
"""
import argparse
import json
import pathlib
import sys

# 确保以项目根目录为工作目录，避免相对路径与 import 问题
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import dotenv
dotenv.load_dotenv(_PROJECT_ROOT / "agentTest" / ".env")

from agentTest.db.metadata_scope import is_allowed_table
from agentTest.metadata.mysql_store import (
    init_metadata_tables, list_evaluated_dialogues, delete_evaluated_dialogue,
)
from agentTest.langchain_app.vectorstores.example_vector_store import ExampleVectorStore


def _table_allowed(db_table: str) -> bool:
    """判断 db.table（或裸表名）是否在当前白名单内；空标识默认保留，避免误删。"""
    identifier = str(db_table or "").strip()
    if not identifier:
        return True
    if "." in identifier:
        db_name, _, table_name = identifier.partition(".")
    else:
        db_name, table_name = "", identifier
    return is_allowed_table(table_name, db_name)


def _tables_have_out_of_scope(tables) -> bool:
    """tables 列表（db.table 或裸表名）是否存在白名单外的表。"""
    for table in tables or []:
        if not _table_allowed(table):
            return True
    return False


def prune_mysql(dry_run: bool = True) -> int:
    """清理 evaluated_dialogues 中引用了白名单外表的记录，返回待删/已删条数。"""
    rows = list_evaluated_dialogues()
    targets = []
    for row in rows:
        tables_used = [
            table.strip() for table in (row["tables_used"] or "").split(",")
            if table.strip()
        ]
        if tables_used and _tables_have_out_of_scope(tables_used):
            targets.append(row)

    for row in targets:
        if dry_run:
            print(f"  [dry-run] 评估记录 id={row['id']} tables_used={row['tables_used']}，待删除")
        else:
            delete_evaluated_dialogue(row["id"])
            print(f"  评估记录 id={row['id']} 已删除（tables_used={row['tables_used']}）")
    return len(targets)


def prune_faiss(dry_run: bool = True) -> int:
    """清理 example_faiss_index 中引用了白名单外表的示例文档，返回待删/已删条数。"""
    from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
    store = ExampleVectorStore(BailianEmbeddings())
    doc_ids = store.find_out_of_scope_doc_ids(_table_allowed)
    if not doc_ids:
        return 0

    if dry_run:
        for doc_id in doc_ids:
            doc = store._vector_store.docstore.search(doc_id)
            question = (doc.metadata or {}).get("question", "")
            tables = (doc.metadata or {}).get("tables", "")
            print(f"  [dry-run] 示例文档 {doc_id} 问题={question[:40]} tables={tables}，待删除")
    else:
        removed = store.remove_doc_ids(doc_ids)
        print(f"  示例文档已删除 {removed} 条并落盘")
    return len(doc_ids)


def main() -> int:
    parser = argparse.ArgumentParser(description="按当前白名单清理优秀案例残留（MySQL + FAISS）")
    parser.add_argument("--dry-run", action="store_true", help="只打印待清理清单，不实际删除")
    args = parser.parse_args()

    init_metadata_tables()
    print("按当前白名单清理优秀案例（多退）...")
    print(f"模式: {'dry-run（仅预览）' if args.dry_run else '实际清理'}")

    n_mysql = prune_mysql(dry_run=args.dry_run)
    n_faiss = prune_faiss(dry_run=args.dry_run)
    print(f"完成：MySQL 待清理 {n_mysql} 条，FAISS 待清理 {n_faiss} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
