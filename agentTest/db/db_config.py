import os

import dotenv

dotenv.load_dotenv()

def get_mysql_config():
    host = os.getenv("MYSQL_HOST")
    port_raw = os.getenv("MYSQL_PORT", "3306")
    user = os.getenv("MYSQL_USER")
    password = os.getenv("MYSQL_PASSWORD")
    database = os.getenv("MYSQL_DATABASE")
    charset = os.getenv("MYSQL_CHARSET", "utf8mb4")

    missing_fields = []

    if not host:
        missing_fields.append("MYSQL_HOST")
    if not user:
        missing_fields.append("MYSQL_USER")
    if not database:
        missing_fields.append("MYSQL_DATABASE")

    if missing_fields:
        raise ValueError(
            f"missing MySQL config: {', '.join(missing_fields)}"
        )

    try:
        port = int(port_raw)
    except ValueError:
        raise ValueError("MYSQL_PORT must be an integer")

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password or "",
        "database": database,
        "charset": charset,
    }