# -*- coding: utf-8 -*-
# 一次性迁移脚本：把 example 优秀案例索引从 L2 重建为真实余弦（IP + 归一化）
# 用法: python -m agentTest.scripts.rebuild_example_index
import os, sys, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
from agentTest.langchain_app.vectorstores.example_vector_store import ExampleVectorStore
from agentTest.langchain_app.vectorstores.example_vector_store import _CACHE_EXAMPLE_DIR


def main():
    embedding = BailianEmbeddings()
    old = ExampleVectorStore(embedding)
    old._ensure_loaded()
    docs = [
        doc for doc in old._vector_store.docstore._dict.values()
        if not doc.metadata.get("_placeholder")
    ]
    # 清除旧假相似度残留，重建后由 search_similar 重新计算
    for doc in docs:
        doc.metadata.pop("_similarity", None)
    print(f"导出旧 example 索引 {len(docs)} 条")
    if os.path.exists(_CACHE_EXAMPLE_DIR):
        shutil.rmtree(_CACHE_EXAMPLE_DIR)
    new = ExampleVectorStore(embedding)
    new._ensure_loaded()
    if docs:
        new._vector_store.add_documents(docs)
        new._save_to_disk()
    print(f"迁移完成: 新索引 {new._vector_store.index.ntotal} 条, 类型 {type(new._vector_store.index).__name__}")


if __name__ == "__main__":
    main()
