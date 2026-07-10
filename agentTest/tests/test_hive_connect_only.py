# 该文件用于做 Hive 最小连接验证，只测试能否建连接并执行最简单的 select 1。
from pyhive import hive
from agentTest.db.hive_config import get_hive_config
import dotenv

dotenv.load_dotenv()

def run_hive_connect_only_test():
    config = get_hive_config()

    print("=" * 60)
    print("开始测试组: hive_connect_only")
    print("=" * 60)
    print(f"host={config['host']}, port={config['port']}, database={config['database']}")

    conn = hive.Connection(
        host=config["host"],
        port=config["port"],
        username=config["username"],
        password=config["password"],
        database=config["database"],
        auth=config["auth"],
    )

    cursor = conn.cursor()
    try:
        cursor.execute("select 1")
        rows = cursor.fetchall()
        print("[PASS] Hive 最小连接测试")
        print(rows)
    finally:
        cursor.close()
        conn.close()

run_hive_connect_only_test()