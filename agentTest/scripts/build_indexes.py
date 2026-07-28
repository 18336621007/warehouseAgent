# 构建/重建所有 FAISS 向量索引
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


def main():
    parser = argparse.ArgumentParser(description="构建/重建 FAISS 向量索引")
    parser.add_argument("--force", action="store_true", help="强制重建（删除已有缓存）")
    parser.add_argument("--target", choices=list(BUILDERS.keys()), help="只构建指定索引")
    args = parser.parse_args()

    print("=" * 60)
    print("FAISS 向量索引构建器")
    print(f"模式: {'强制重建' if args.force else '增量构建（已有则加载）'}")
    print("=" * 60)

    embedding = BailianEmbeddings()
    targets = [args.target] if args.target else list(BUILDERS.keys())

    for name in targets:
        cache_dir = CACHE_DIRS[name]
        builder = BUILDERS[name]

        if args.force and os.path.exists(cache_dir):
            print(f"\n[{name}] 删除已有缓存: {cache_dir}")
            shutil.rmtree(cache_dir)

        try:
            print(f"\n[{name}] 开始构建...")
            result = builder(embedding)
            doc_count = len(result.get("documents", []))
            print(f"[{name}] 完成: {doc_count} 条记录 -> {cache_dir}")
        except Exception as e:
            print(f"[{name}] 失败: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*60}")
    print("构建完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
