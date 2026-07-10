import os


def get_hive_config():
    # 读取 Hive 连接配置，后续统一由数据源和 metadata 提供者复用
    return {
        "host": os.getenv("HIVE_HOST", ""),
        "port": int(os.getenv("HIVE_PORT", "10000")),
        "username": os.getenv("HIVE_USERNAME", ""),
        "password": os.getenv("HIVE_PASSWORD", ""),
        "database": os.getenv("HIVE_DATABASE", "test"),
        "auth": os.getenv("HIVE_AUTH", "LDAP")
    }