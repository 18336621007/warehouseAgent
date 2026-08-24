# BM25 索引一键同步脚本
# 与 FAISS 索引同步脚本 build_indexes.py 配套使用
# 用法:
#   python -m agentTest.scripts.build_bm25_index           # 构建全部
#   python -m agentTest.scripts.build_bm25_index --force # 强制重建
import os
import sys
import shutil
import argparse

# 添加项目根目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_build_bm25(force: bool = False) -> None:
    """构建/同步 BM25 索引"""
    from agentTest.langchain_app.app_builder import build_bm25_rag, _CACHE_BM25_DIR

    print("=" * 60)
    print("BM25 倒排索引同步器")
    print(f"模式: {'强制重建' if force else '增量同步（新增/更新）'}")
    print("=" * 60)

    try:
        result = build_bm25_rag(force_rebuild=force, return_stats=True)
        bm25 = result["retriever"]
        docs = result["documents"]
        doc_sources = result.get("doc_sources", {})

        print(f"\n[bm25] 文档总数: {len(docs)} 条")
        print(f"[bm25] 索引缓存: {_CACHE_BM25_DIR}")

        # 显示各层级文档数量
        if doc_sources:
            print("\n[bm25] 各层级文档分布:")
            for name, docs_list in doc_sources.items():
                print(f"  - {name}: {len(docs_list)} 条")

        # 词汇统计
        vocab_size = bm25.get_vocabulary_size()
        print(f"\n[bm25] 词表大小: {vocab_size} 个词")

        print(f"\n{'='*60}")
        print("同步完成")
        print(f"{'='*60}")

    except Exception as e:
        print(f"\n[bm25] 构建失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="构建/同步 BM25 倒排索引",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m agentTest.scripts.build_bm25_index           # 增量同步
  python -m agentTest.scripts.build_bm25_index --force # 强制重建

说明:
  - 增量同步: 只构建新增的文档，已有索引直接加载
  - 强制重建: 删除所有缓存，重新构建全部索引
        """
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重建（删除已有缓存，重新构建全部索引）"
    )
    args = parser.parse_args()

    run_build_bm25(force=args.force)


if __name__ == "__main__":
    main()
