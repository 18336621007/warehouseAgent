import os


def get_hive_timeout_seconds() -> int:
    """Hive 连接/查询 socket 超时秒数（环境变量 HIVE_TIMEOUT_SECONDS，默认 30）。
    防止 Hive 无响应时 pyhive 无限等待挂起进程。"""
    try:
        return max(int(os.getenv("HIVE_TIMEOUT_SECONDS", "30")), 1)
    except ValueError:
        return 30


def apply_thrift_socket_timeout(conn, timeout_seconds: int = None) -> bool:
    """给 pyhive 底层 thrift transport 设置 socket 超时（兼容 Buffered/SASL 包装）。
    超时后 socket 读写抛异常，由调用方捕获跳过，避免无限挂起。
    返回是否成功设置到 socket。"""
    if timeout_seconds is None:
        timeout_seconds = get_hive_timeout_seconds()
    timeout_ms = int(timeout_seconds * 1000)
    transport = getattr(conn, "_transport", None)
    seen = set()
    while transport is not None and id(transport) not in seen:
        seen.add(id(transport))
        # 找到最底层的 thrift socket（TSocket 提供 setTimeout）
        if hasattr(transport, "setTimeout"):
            try:
                transport.setTimeout(timeout_ms)
                return True
            except Exception:
                return False
        nxt = None
        for attr in ("_transport", "_trans", "_socket"):
            if hasattr(transport, attr):
                nxt = getattr(transport, attr)
                break
        transport = nxt
    return False


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