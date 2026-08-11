# 构建/同步所有 FAISS 向量索引（M3：按唯一键 upsert/delete）
# 用法: python -m agentTest.scripts.build_indexes
# 选项: --force  强制重建（删除已有缓存）
import os, sys, shutil, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
from agentTest.langchain_app.app_builder import (
    _CACHE_DB_DIR, _CACHE_TABLE_DIR, _CACHE_COLUMN_DIR,
    _CACHE_ENRICHED_DIR, _CACHE_SCHEMA_DIR,
    build_db_rag, build_table_rag, build_column_rag,
    build_enriched_schema_rag_app,
)

CACHE_DIRS = {
    "db":       _CACHE_DB_DIR,
    "table":    _CACHE_TABLE_DIR,
    "column":   _CACHE_COLUMN_DIR,
    "enriched": _CACHE_ENRICHED_DIR,
    "schema":   _CACHE_SCHEMA_DIR,
}

BUILDERS = {
    "db":       build_db_rag,
    "table":    build_table_rag,
    "column":   build_column_rag,
    "enriched": build_enriched_schema_rag_app,
}


def run_build_all(force: bool = False, target: str | None = None, embedding=None):
    """构建/同步全部或指定 FAISS 索引（供 CLI 与 sync_metadata.py 复用）"""
    print("=" * 60)
    print("FAISS 向量索引同步器")
    print(f"模式: {'强制重建' if force else '增量同步（新增/更新/删除）'}")
    print("=" * 60)

    if embedding is None:
        embedding = BailianEmbeddings()
    targets = [target] if target else list(BUILDERS.keys())

    for name in targets:
        cache_dir = CACHE_DIRS[name]
        builder = BUILDERS[name]

        if force and os.path.exists(cache_dir):
            print(f"\n[{name}] 删除已有缓存: {cache_dir}")
            shutil.rmtree(cache_dir)

        try:
            print(f"\n[{name}] 开始同步...")
            result = builder(embedding, force_rebuild=force, return_stats=True)
            doc_count = len(result.get("documents", []))
            stats = result.get("sync_stats") or {}
            print(f"[{name}] 文档总数: {doc_count} 条 -> {cache_dir}")
            if stats.get("rebuilt"):
                print(f"[{name}] 本次变更: 全量重建 {doc_count} 条")
            else:
                added = stats.get("added", 0)
                changed = stats.get("changed", 0)
                removed = stats.get("removed", 0)
                if added or changed or removed:
                    print(f"[{name}] 本次变更: 新增 {added} / 更新 {changed} / 删除 {removed}")
                else:
                    print(f"[{name}] 本次变更: 无变化，直接加载")
        except Exception as e:
            print(f"[{name}] 失败: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*60}")
    print("同步完成")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="构建/同步 FAISS 向量索引")
    parser.add_argument("--force", action="store_true", help="强制重建（删除已有缓存）")
    parser.add_argument("--target", choices=list(BUILDERS.keys()), help="只构建指定索引")
    args = parser.parse_args()
    run_build_all(force=args.force, target=args.target)


if __name__ == "__main__":
    main()

