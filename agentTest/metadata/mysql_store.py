"""MySQL 元数据存储层，负责 enriched_metadata 的读写和断点续跑，每次调用都会对比mysql中已有的表集合，增量更新"""
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


def _column_exists_in_table(cursor, table_name: str, column_name: str) -> bool:
    """检查表是否存在某列，用于幂等升级表结构（兼容历史库）"""
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table_name, column_name)
    )
    return cursor.fetchone()[0] > 0


def list_enriched_table_names():
    """返回 MySQL 中已有的表名集合，用于增量比对"""
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT full_name FROM enriched_tables")
            return {row[0] for row in cursor.fetchall()}
    finally:
        conn.close()


def delete_table_data(full_name: str):
    """删除某张表的增强数据（表级+字段级），用于表结构变更后重采"""
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM enriched_tables WHERE full_name = %s", (full_name,))
            # 字段级按 database.table 前缀匹配删除
            cursor.execute(
                "DELETE FROM enriched_columns WHERE CONCAT(database_name, '.', table_name) = %s",
                (full_name,)
            )
        conn.commit()
    finally:
        conn.close()

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
                    meta_source VARCHAR(20) DEFAULT 'llm_enhanced',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # 兼容历史库：enriched_columns 补充 meta_source 列并回填来源标记
            if not _column_exists_in_table(cursor, "enriched_columns", "meta_source"):
                cursor.execute(
                    "ALTER TABLE enriched_columns "
                    "ADD COLUMN meta_source VARCHAR(20) DEFAULT 'llm_enhanced'"
                )
            cursor.execute(
                "UPDATE enriched_columns SET meta_source = 'ddl_comment' "
                "WHERE original_comment IS NOT NULL AND original_comment != ''"
            )
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
                     sample_values, original_comment, meta_source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    domain = VALUES(domain),
                    fields_type = VALUES(fields_type),
                    relations = VALUES(relations),
                    field_aliases = VALUES(field_aliases),
                    sample_values = VALUES(sample_values),
                    original_comment = VALUES(original_comment),
                    meta_source = VALUES(meta_source)
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
                data.get("meta_source", ""),
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
def load_enriched_tables():
    """从 MySQL 加载表级增强元数据，返回 {full_name: {domain, core_function, key_entities, potential_use_cases, original_comment}}"""
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT full_name, domain, core_function, key_entities, "
                "potential_use_cases, original_comment FROM enriched_tables"
            )
            rows = cursor.fetchall()

        result = {}
        for row in rows:
            result[row[0]] = {
                "domain": row[1] or "",
                "core_function": row[2] or "",
                "key_entities": json.loads(row[3]) if row[3] else [],
                "potential_use_cases": json.loads(row[4]) if row[4] else [],
                "original_comment": row[5] or "",
            }
        return result
    finally:
        conn.close()

# 简要注释：从 MySQL 加载库级增强元数据，返回 {database_name: {domain, description, full_table_list}}
def load_enriched_databases():
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT database_name, domain, full_table_list, description FROM enriched_databases"
            )
            rows = cursor.fetchall()

        result = {}
        for row in rows:
            result[row[0]] = {
                "domain": row[1] or "",
                "full_table_list": json.loads(row[2]) if row[2] else [],
                "description": row[3] or "",
            }
        return result
    finally:
        conn.close()


# 简要注释：从 MySQL 加载字段级增强元数据，返回 [{full_key, database_name, table_name, column_name, fields_type, field_aliases, sample_values, relations, original_comment, meta_source}]
def load_enriched_columns():
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            try:
                cursor.execute(
                    "SELECT full_key, database_name, table_name, column_name, "
                    "fields_type, field_aliases, sample_values, relations, original_comment, meta_source "
                    "FROM enriched_columns"
                )
                has_meta_source = True
            except Exception:
                # 兼容旧库未执行 ALTER 补列：回退旧查询，meta_source 由 original_comment 推导
                conn.rollback()
                cursor.execute(
                    "SELECT full_key, database_name, table_name, column_name, "
                    "fields_type, field_aliases, sample_values, relations, original_comment "
                    "FROM enriched_columns"
                )
                has_meta_source = False
            rows = cursor.fetchall()

        result = []
        for row in rows:
            meta_source = (row[9] or "") if has_meta_source else ""
            if not meta_source:
                meta_source = "ddl_comment" if (row[8] or "") else "llm_enhanced"
            result.append({
                "full_key": row[0],
                "database_name": row[1] or "",
                "table_name": row[2] or "",
                "column_name": row[3] or "",
                "fields_type": row[4] or "dimension",
                "field_aliases": json.loads(row[5]) if row[5] else [],
                "sample_values": json.loads(row[6]) if row[6] else [],
                "relations": json.loads(row[7]) if row[7] else [],
                "original_comment": row[8] or "",
                "meta_source": meta_source,
            })
        return result
    finally:
        conn.close()

# ── Evaluator 高质量对话存储 ────────────────────────────────────────

def init_evaluator_table():
    """建表（幂等），存储 Evaluator 评估后的对话，user_score 支持用户后续打分"""
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS evaluated_dialogues (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    question TEXT,
                    resolved_question TEXT,
                    final_sql TEXT,
                    final_answer TEXT,
                    tables_used VARCHAR(500) DEFAULT '',
                    fields_used VARCHAR(500) DEFAULT '',
                    domain_tag VARCHAR(100) DEFAULT '',
                    advisor_turns INT DEFAULT 0,
                    total_time_ms FLOAT DEFAULT 0,
                    time_score FLOAT DEFAULT 0,
                    turn_score FLOAT DEFAULT 0,
                    llm_self_score FLOAT DEFAULT 0,
                    user_score FLOAT DEFAULT 75,
                    comprehensive_score FLOAT DEFAULT 0,
                    is_high_quality TINYINT(1) DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            # 兼容旧表：补充缺失字段
            for col, col_def in [
                ("resolved_question", "TEXT AFTER question"),
                ("effective_query", "TEXT AFTER question"),
                ("user_score", "FLOAT DEFAULT 75 AFTER llm_self_score"),
                ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
                ("example_hash", "VARCHAR(32) DEFAULT '' AFTER is_high_quality"),
            ]:
                cursor.execute("""
                    SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'evaluated_dialogues'
                      AND COLUMN_NAME = %s
                """, (col,))
                if cursor.fetchone()[0] == 0:
                    cursor.execute(f"ALTER TABLE evaluated_dialogues ADD COLUMN {col} {col_def}")
        conn.commit()
    finally:
        conn.close()


def save_evaluated_dialogue(
    question: str,
    resolved_question: str,
    sql: str,
    answer: str,
    tables_used: list,
    fields_used: list,
    advisor_turns: int,
    total_time_ms: float,
    time_score: float,
    turn_score: float,
    llm_self_score: float,
    comprehensive_score: float,
    domain_tag: str = "",
    user_score: float = 75,
    example_hash: str = "",
    effective_query: str = "",
):
    """保存一条评估后的对话记录，始终入库并返回 ID"""
    is_high_quality = 1 if comprehensive_score >= 80 else 0
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO evaluated_dialogues
                    (question, effective_query, resolved_question, final_sql, final_answer, tables_used, fields_used,
                     domain_tag, advisor_turns, total_time_ms, time_score, turn_score,
                     llm_self_score, user_score, comprehensive_score, is_high_quality, example_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                question, effective_query, resolved_question, sql, answer,
                ",".join(tables_used) if tables_used else "",
                ",".join(fields_used) if fields_used else "",
                domain_tag, advisor_turns, total_time_ms, time_score, turn_score,
                llm_self_score, user_score, comprehensive_score, is_high_quality,
                example_hash,
            ))
            dialogue_id = cursor.lastrowid
        conn.commit()
        return dialogue_id
    finally:
        conn.close()


def update_user_score(dialogue_id: int, user_score: float):
    """用户打分后更新 MySQL，重算综合分并刷新 is_high_quality
    返回 dict：{ was_high, is_high, hash_id, question, sql, answer, tables, fields, domain, score }
    供 FAISS 同步：原先高分变低分则删，原先低分变高分则加"""
    from agentTest.config.evaluator import (
        WEIGHT_TIME, WEIGHT_TURNS, WEIGHT_LLM_SELF, WEIGHT_USER,
        HIGH_QUALITY_THRESHOLD,
    )
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            # 读取现有分数及 FAISS 同步所需字段
            cursor.execute(
                """SELECT time_score, turn_score, llm_self_score, is_high_quality,
                          example_hash, effective_query, resolved_question, final_sql, final_answer,
                          tables_used, fields_used, domain_tag, comprehensive_score
                   FROM evaluated_dialogues WHERE id = %s""",
                (dialogue_id,)
            )
            row = cursor.fetchone()
            if not row:
                return {}
            time_s, turn_s, llm_s, old_high = row[0], row[1], row[2], row[3]
            old_hash = row[4] or ''
            old_effective = row[5] or ''
            old_q = row[6] or ''
            old_sql = row[7] or ''
            old_answer = row[8] or ''
            old_tables = row[9] or ''
            old_fields = row[10] or ''
            old_domain = row[11] or ''

            # 用真实用户分重算综合分
            comprehensive = round(
                WEIGHT_TIME * (time_s or 0)
                + WEIGHT_TURNS * (turn_s or 0)
                + WEIGHT_LLM_SELF * (llm_s or 0)
                + WEIGHT_USER * user_score,
                1,
            )
            is_high = 1 if comprehensive >= HIGH_QUALITY_THRESHOLD else 0
            was_high = bool(old_high)

            cursor.execute(
                "UPDATE evaluated_dialogues SET user_score = %s, comprehensive_score = %s, is_high_quality = %s WHERE id = %s",
                (user_score, comprehensive, is_high, dialogue_id),
            )
        conn.commit()

        return {
            "was_high": was_high,
            "is_high": bool(is_high),
            "hash_id": old_hash,
            "question": old_q,
            "sql": old_sql,
            "answer": old_answer,
            "tables": [t.strip() for t in old_tables.split(",") if t.strip()] if old_tables else [],
            "fields": [f.strip() for f in old_fields.split(",") if f.strip()] if old_fields else [],
            "domain_tag": old_domain,
            "effective_query": old_effective,
            "score": comprehensive,
        }
    finally:
        conn.close()
