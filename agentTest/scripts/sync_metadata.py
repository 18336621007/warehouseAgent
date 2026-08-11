# -*- coding: utf-8 -*-
"""sync_metadata.py — M2/M3 一键同步：采集(diff) + 增强 + 向量库同步

用法（python 命令）：
  python -m agentTest.scripts.sync_metadata                       # 增量同步（指纹一致跳过 + 向量 upsert/delete）
  python -m agentTest.scripts.sync_metadata --force-table         # 强制重跑表级/库级增强后同步向量（字段级复用 MySQL 结果）
  python -m agentTest.scripts.sync_metadata --force               # 强制重建向量索引
  python -m agentTest.scripts.sync_metadata --skip-vector         # 只更新 MySQL，不同步向量库
  python -m agentTest.scripts.sync_metadata --dry-run             # 只打印将执行的步骤，不实际执行
"""
import argparse
import sys

# 确保以项目根目录为工作目录，避免相对路径与 import 问题
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(description="M2/M3 元数据增量采集 + 向量库同步")
    parser.add_argument("--force-table", action="store_true", help="强制重跑表级/库级增强后同步向量库（字段级复用 MySQL 现有结果）")
    parser.add_argument("--force", action="store_true", help="向量索引强制重建")
    parser.add_argument("--skip-vector", action="store_true", help="只更新 MySQL，不同步向量库")
    parser.add_argument("--dry-run", action="store_true", help="仅打印将执行的步骤，不实际执行")
    args = parser.parse_args()

    if args.dry_run:
        if args.force_table:
            print("步骤1: metadata_enricher 强制重跑表级/库级增强（字段级复用 MySQL 结果）")
        else:
            print("步骤1: metadata_enricher 增量采集（schema 指纹 diff）")
        print("步骤2: build_indexes 同步 FAISS 向量索引（新增/更新/删除）")
        return 0

    # 步骤1: 采集 + 增强（M2 指纹增量，写入 MySQL）
    print("=" * 60)
    print("Step 1/2: 元数据采集与增强（schema 指纹增量）")
    print("=" * 60)
    from agentTest.metadata.metadata_enricher import build_enriched_metadata
    build_enriched_metadata(force_tables=args.force_table)

    # 步骤2: 向量库同步（M3 upsert/delete）
    if not args.skip_vector:
        print("=" * 60)
        print("Step 2/2: 向量索引同步")
        print("=" * 60)
        from agentTest.scripts.build_indexes import run_build_all
        run_build_all(force=args.force)

    print("同步完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
