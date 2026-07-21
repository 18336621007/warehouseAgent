"""MySQL 元数据存储层，负责 enriched_metadata 的读写和断点续跑"""
import json
import pymysql

from agentTest.db.db_config import get_mysql_config


def _get_connection():
    """获取 MySQL 连接"""
    cfg = get_mysql_config()
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset=cfg["charset"],
    )


def init_metadata_tables():
    """建库建表，幂等执行（表不存在才建）"""
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            # 数据库不存在则创建
            cfg = get_mysql_config()
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{cfg['database']}` "
                f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cursor.execute(f"USE `{cfg['database']}`")

            # 库级增强表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS enriched_databases (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    database_name VARCHAR(100) NOT NULL UNIQUE,
                    domain VARCHAR(100) DEFAULT '',
                    full_table_list JSON,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # 表级增强表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS enriched_tables (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    full_name VARCHAR(200) NOT NULL UNIQUE,
                    domain VARCHAR(100) DEFAULT '',
                    core_function TEXT,
                    key_entities JSON,
                    potential_use_cases JSON,
                    original_comment VARCHAR(500) DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # 字段级增强表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS enriched_columns (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    full_key VARCHAR(300) NOT NULL UNIQUE,
                    database_name VARCHAR(100) DEFAULT '',
                    table_name VARCHAR(100) DEFAULT '',
                    column_name VARCHAR(100) DEFAULT '',
                    domain VARCHAR(100) DEFAULT '',
                    fields_type VARCHAR(20) DEFAULT 'dimension',
                    relations JSON,
                    field_aliases JSON,
                    sample_values JSON,
                    original_comment VARCHAR(500) DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.commit()
    finally:
        conn.close()


# ── 存在性检查（断点续跑核心） ────────────────────────────────────────
def column_exists(full_key: str) -> bool:
    """检查某个字段是否已经增强过"""
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM enriched_columns WHERE full_key = %s",
                (full_key,)
            )
            return cursor.fetchone()[0] > 0
    finally:
        conn.close()


def table_exists(full_name: str) -> bool:
    """检查某张表是否已经增强过"""
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM enriched_tables WHERE full_name = %s",
                (full_name,)
            )
            return cursor.fetchone()[0] > 0
    finally:
        conn.close()


# ── 写入操作（upsert） ────────────────────────────────────────────────
def save_column(full_key: str, database_name: str, table_name: str,
                column_name: str, data: dict, samples: list):
    """保存字段增强结果，已存在则更新"""
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO enriched_columns
                    (full_key, database_name, table_name, column_name,
                     domain, fields_type, relations, field_aliases,
                     sample_values, original_comment)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    domain = VALUES(domain),
                    fields_type = VALUES(fields_type),
                    relations = VALUES(relations),
                    field_aliases = VALUES(field_aliases),
                    sample_values = VALUES(sample_values),
                    original_comment = VALUES(original_comment)
            """, (
                full_key,
                database_name,
                table_name,
                column_name,
                data.get("domain", ""),
                data.get("fields_type", "dimension"),
                json.dumps(data.get("relations", []), ensure_ascii=False),
                json.dumps(data.get("field_aliases", []), ensure_ascii=False),
                json.dumps(samples, ensure_ascii=False),
                data.get("_original_comment", ""),
            ))
        conn.commit()
    finally:
        conn.close()


def save_table(full_name: str, data: dict):
    """保存表增强结果，已存在则更新"""
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO enriched_tables
                    (full_name, domain, core_function, key_entities,
                     potential_use_cases, original_comment)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    domain = VALUES(domain),
                    core_function = VALUES(core_function),
                    key_entities = VALUES(key_entities),
                    potential_use_cases = VALUES(potential_use_cases),
                    original_comment = VALUES(original_comment)
            """, (
                full_name,
                data.get("domain", ""),
                data.get("core_function", ""),
                json.dumps(data.get("key_entities", []), ensure_ascii=False),
                json.dumps(data.get("potential_use_cases", []), ensure_ascii=False),
                data.get("_original_comment", ""),
            ))
        conn.commit()
    finally:
        conn.close()


def save_database(database_name: str, data: dict):
    """保存库增强结果，已存在则更新"""
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO enriched_databases
                    (database_name, domain, full_table_list, description)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    domain = VALUES(domain),
                    full_table_list = VALUES(full_table_list),
                    description = VALUES(description)
            """, (
                database_name,
                data.get("domain", ""),
                json.dumps(data.get("full_table_list", []), ensure_ascii=False),
                data.get("description", ""),
            ))
        conn.commit()
    finally:
        conn.close()