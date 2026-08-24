# BM25 混合检索测试脚本
# 用法: python -m agentTest.scripts.test_bm25
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentTest.langchain_app.embeddings.bailian_embeddings import BailianEmbeddings
from agentTest.langchain_app.app_builder import build_bm25_rag, build_db_rag, build_table_rag, build_column_rag
from agentTest.langchain_app.rag.hybrid_retriever import HybridRetriever
from agentTest.langchain_app.rag.bm25_retriever import BM25Retriever
from langchain_core.documents import Document


def test_bm25_only():
    """测试纯 BM25 检索"""
    print("\n" + "=" * 60)
    print("测试 1: 纯 BM25 检索")
    print("=" * 60)

    # 构建 BM25 索引
    bm25_rag = build_bm25_rag(force_rebuild=False)
    bm25_retriever = bm25_rag["retriever"]

    print(f"BM25 索引文档总数: {bm25_retriever.index_size}")
    print(f"BM25 词表大小: {bm25_retriever.get_vocabulary_size()}")

    test_queries = [
        "新增订单"
    ]

    for query in test_queries:
        print(f"\n--- 查询: '{query}' ---")
        results = bm25_retriever.retrieve(query, top_k=3)
        for i, (doc, score) in enumerate(results, 1):
            table = doc.metadata.get("table", "unknown")
            column = doc.metadata.get("column", "")
            content = doc.page_content.replace("\n", " ")
            print(f"  {i}. [{table}.{column}] score={score:.4f}")
            print(f"     {content}")


def test_vector_only():
    """测试纯向量检索"""
    print("\n" + "=" * 60)
    print("测试 2: 纯向量检索")
    print("=" * 60)

    embedding = BailianEmbeddings()

    # 构建 FAISS 索引
    db_rag = build_db_rag(embedding)
    table_rag = build_table_rag(embedding)
    column_rag = build_column_rag(embedding)

    test_queries = [
        "新增订单"
    ]

    for query in test_queries:
        print(f"\n--- 查询: '{query}' ---")
        docs = column_rag["vector_store"].similarity_search(query, k=3)
        for i, doc in enumerate(docs, 1):
            table = doc.metadata.get("table", "unknown")
            column = doc.metadata.get("column", "")
            content = doc.page_content.replace("\n", " ")
            print(f"  {i}. [{table}.{column}]")
            print(f"     {content}")


def test_hybrid():
    """测试混合检索"""
    print("\n" + "=" * 60)
    print("测试 3: 混合检索 (BM25 + 向量)")
    print("=" * 60)

    embedding = BailianEmbeddings()

    # 构建所有索引
    bm25_rag = build_bm25_rag(force_rebuild=False)
    db_rag = build_db_rag(embedding)
    table_rag = build_table_rag(embedding)
    column_rag = build_column_rag(embedding)

    # 创建混合检索器
    hybrid = HybridRetriever(
        bm25_retriever=bm25_rag["retriever"],
        vector_stores={
            "db": db_rag["vector_store"],
            "table": table_rag["vector_store"],
            "column": column_rag["vector_store"],
        },
        alpha=0.3,  # 30% BM25 + 70% 向量
    )

    print(f"当前 BM25 权重 alpha = {hybrid.get_alpha()}")
    print(f"BM25 索引文档数: {bm25_rag['retriever'].index_size}")

    test_queries = [
        "新增订单"
    ]

    for query in test_queries:
        print(f"\n--- 查询: '{query}' ---")
        results = hybrid._search(query, k=5, vector_store_key="column")
        for i, (doc, score) in enumerate(results, 1):
            table = doc.metadata.get("table", "unknown")
            column = doc.metadata.get("column", "")
            content = doc.page_content.replace("\n", " ")
            print(f"  {i}. [{table}.{column}] score={score:.4f}")
            print(f"     {content}")


def test_alpha_comparison():
    """对比不同 alpha 值的检索结果"""
    print("\n" + "=" * 60)
    print("测试 4: 不同 BM25 权重对比")
    print("=" * 60)

    embedding = BailianEmbeddings()

    # 构建索引
    bm25_rag = build_bm25_rag(force_rebuild=False)
    column_rag = build_column_rag(embedding)

    query = "订单金额"

    for alpha in [0.0, 0.3, 0.5, 1.0]:
        print(f"\n--- alpha = {alpha} ---")
        hybrid = HybridRetriever(
            bm25_retriever=bm25_rag["retriever"],
            vector_stores={"column": column_rag["vector_store"]},
            alpha=alpha,
        )
        results = hybrid._search(query, k=3, vector_store_key="column")
        for i, (doc, score) in enumerate(results, 1):
            column = doc.metadata.get("column", "")
            print(f"  {i}. [{column}] score={score:.4f}")


def main():
    print("=" * 60)
    print("BM25 混合检索测试")
    print("=" * 60)

    print("\n请选择测试项：")
    print("  1 - 纯 BM25 检索")
    print("  2 - 纯向量检索")
    print("  3 - 混合检索 (BM25 + 向量)")
    print("  4 - 不同权重对比")
    print("  a - 运行全部测试")
    print("  q - 退出")

    choice = input("\n请输入选项: ").strip().lower()

    if choice == "1":
        test_bm25_only()
    elif choice == "2":
        test_vector_only()
    elif choice == "3":
        test_hybrid()
    elif choice == "4":
        test_alpha_comparison()
    elif choice == "a":
        test_bm25_only()
        test_vector_only()
        test_hybrid()
        test_alpha_comparison()
    elif choice == "q":
        return
    else:
        print("无效选项")


if __name__ == "__main__":
    main()
# --- 查询: '新增订单' ---
# 仅BM25
#   1. [ads_trip.ads_exchange_platform_operations_report_day.new_z_order] score=9.0402
#      字段: new_z_order 别名: 新增Z类订单、Z类推广新增单、新增Z推广单、Z类新订单、Z推广新增订单  原始备注: 新增Z类推广订单
#   2. [ads_trip.ads_exchange_order_device_info_day.order_new_com] score=9.0399
#      字段: order_new_com 别名: 新增订单量、全量新增订单数、日新增订单数、新增订单总数、订单新增数  原始备注: 新增订单数（全量）(分区维度)
#   3. [ads_trip.ads_exchange_platform_operations_report_day.new_nature_order] score=8.9765
#      字段: new_nature_order 别名: 新增自然订单、自然流量新增订单、自然新增订单数、自然渠道新订单、平台自然增长订单  原始备注: 新增自然流量订单

#仅向量检索
# 1. [ads_trip.ads_exchange_order_device_info_day.order_new_com]
#      字段: order_new_com 别名: 新增订单量、全量新增订单数、日新增订单数、新增订单总数、订单新增数 原始备注: 新增订单数（全量）(分区维度)
# 2. [dws_trip.dm_exchange_order_overdue_info_hour.new_overdue_order_num]
#      字段: new_overdue_order_num 类型: 【度量】 别名: new_overdue_order_num、纯新用户逾期订单数、新客逾期单量  原始备注: 纯新用户的逾期订单数
# 3. [dws_trip.dm_exchange_order_addition_info_hour.extend_c_addition_order_num]
#      字段: extend_c_addition_order_num 别名: C端推广新增订单数、C渠道新增订单量、C类推广新单数 原始备注: 推广渠道为C的新增订单数

#混合检索
# 1. [ads_trip.ads_exchange_order_device_info_day.order_new_com] score=0.9998
#      字段: order_new_com 别名: 新增订单量、全量新增订单数、日新增订单数、新增订单总数、订单新增数 原始备注: 新增订单数（全量）(分区维度)
# 2. [dws_trip.dm_exchange_order_overdue_info_hour.new_overdue_order_num] score=0.5063
#      字段: new_overdue_order_num 别名: new_overdue_order_num、纯新用户逾期订单数、新客逾期单量 原始备注: 纯新用户的逾期订单数
# 3. [dws_trip.dm_exchange_order_addition_info_hour.extend_c_addition_order_num] score=0.4145
#    字段: extend_c_addition_order_num  别名: C端推广新增订单数、C渠道新增订单量、C类推广新单数 原始备注: 推广渠道为C的新增订单数
# 4. [ads_trip.ads_exchange_platform_operations_report_day.new_z_order] score=0.3000
#    字段: new_z_order 别名: 新增Z类订单、Z类推广新增单、新增Z推广单、Z类新订单、Z推广新增订单 原始备注: 新增Z类推广订单
# 5. [dws_trip.dm_exchange_order_addition_info_hour.new_addition_order_num] score=0.2669
#      字段: new_addition_order_num 类型: 【度量】 别名: 纯新用户新增订单数、新客新增订单量、new_addition_order_num 原始备注: 纯新用户的新增订单数