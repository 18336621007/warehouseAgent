import os

import dotenv

dotenv.load_dotenv()

def get_doris_config():
    # 读取 Doris 连接配置（Doris 走 MySQL 协议，pymysql 直连）
    host = os.getenv("DORIS_HOST", "")
    port = os.getenv("DORIS_PORT", "9030")
    user = os.getenv("DORIS_USER", "")
    password = os.getenv("DORIS_PASSWORD", "")
    charset = os.getenv("DORIS_CHARSET", "utf8mb4")
    connect_timeout = os.getenv("DORIS_CONNECT_TIMEOUT", "10")
    read_timeout = os.getenv("DORIS_READ_TIMEOUT", "120")

    missing_fields = []
    if not host:
        missing_fields.append("DORIS_HOST")
    if not user:
        missing_fields.append("DORIS_USER")
    if missing_fields:
        raise ValueError(f"missing Doris config: {', '.join(missing_fields)}")

    try:
        port = int(port)
        connect_timeout = int(connect_timeout)
        read_timeout = int(read_timeout)
    except ValueError:
        raise ValueError("DORIS_PORT / DORIS_CONNECT_TIMEOUT / DORIS_READ_TIMEOUT must be integers")

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "charset": charset,
        "connect_timeout": connect_timeout,
        "read_timeout": read_timeout,
    }
