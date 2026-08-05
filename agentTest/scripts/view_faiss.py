# 查看所有 FAISS 向量数据库内容
# 用法: python -m agentTest.scripts.view_faiss
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
from langchain_community.vectorstores import FAISS

BASE = os.path.join(os.path.dirname(__file__), "..", "langgraph_app", "cache")

INDEXES = {
    # "db":     os.path.join(BASE, "db_faiss_index"),
    # "table":  os.path.join(BASE, "table_faiss_index"),
    # "column": os.path.join(BASE, "column_faiss_index"),
    "example": os.path.join(BASE, "example_faiss_index"),
    # "enriched": os.path.join(BASE, "enriched_faiss_index"),
    # "schema": os.path.join(BASE, "schema_faiss_index"),
}


def main():
    print("=" * 60)
    print("FAISS 向量数据库内容查看器")
    print("=" * 60)

    embedding = BailianEmbeddings()

    for name, path in INDEXES.items():
        if not os.path.exists(path) or not os.listdir(path):
            print(f"\n[{name}] 不存在或为空")
            continue

        try:
            vs = FAISS.load_local(path, embedding, allow_dangerous_deserialization=True)
            docs = list(vs.docstore._dict.values())
            real = [d for d in docs if not d.metadata.get("_placeholder")]
            print(f"\n{'='*60}")
            print(f"[{name}] {len(real)} 条有效记录 (共 {len(docs)} 条)")
            print(f"{'='*60}")

            for i, doc in enumerate(real):
                meta = doc.metadata
                if name == "example":
                    print(f"  [{i+1}] 问题: {meta.get('question','?')[:80]}")
                    print(f"       SQL:   {meta.get('sql','?')[:100]}")
                    print(f"       表:    {meta.get('tables','?')}")
                    print(f"       字段:  {meta.get('fields','?')}")
                    print(f"       评分:  {meta.get('score','?')}  hash={meta.get('hash_id','?')}")
                elif name == "db":
                    print(f"  [{i+1}] {doc.page_content[:120]}")
                elif name == "table":
                    print(f"  [{i+1}] 表={meta.get('table','?')}  {doc.page_content[:100]}")
                elif name == "column":
                    print(f"  [{i+1}] 字段={meta.get('column','?')}  {doc.page_content[:100]}")
                else:
                    print(f"  [{i+1}] {doc.page_content[:120]}")

        except Exception as e:
            print(f"\n[{name}] 加载失败: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*60}")
    print("查看完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()



