import os

import dotenv

dotenv.load_dotenv()

def get_trino_config():
    # 读取 Trino 连接配置（SSL 连接，SSLVerification=NONE 对应 verify=False）
    host = os.getenv("TRINO_HOST", "")
    port = os.getenv("TRINO_PORT", "8443")
    user = os.getenv("TRINO_USER", "")
    password = os.getenv("TRINO_PASSWORD", "")
    catalog = os.getenv("TRINO_CATALOG", "hive")
    http_scheme = os.getenv("TRINO_HTTP_SCHEME", "https")
    # SSLVerification=NONE -> verify=False（跳过证书校验）；TRUE -> 校验
    verify = os.getenv("TRINO_SSL_VERIFY", "false").strip().lower() in ("1", "true", "yes")
    timeout_seconds = os.getenv("TRINO_TIMEOUT_SECONDS", "120")

    missing_fields = []
    if not host:
        missing_fields.append("TRINO_HOST")
    if not user:
        missing_fields.append("TRINO_USER")
    if missing_fields:
        raise ValueError(f"missing Trino config: {', '.join(missing_fields)}")

    try:
        port = int(port)
        timeout = float(timeout_seconds) if timeout_seconds else None
    except ValueError:
        raise ValueError("TRINO_PORT / TRINO_TIMEOUT_SECONDS must be numeric")

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "catalog": catalog,
        "http_scheme": http_scheme,
        "verify": verify,
        "timeout": timeout,
    }
