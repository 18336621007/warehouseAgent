"""对话记录落盘（MySQL）——方案 A：所有人共享全部会话，creator 仅用于区分不同用户。

两个信息点独立字段存储：
- conversations：会话元信息（创建人/标题/时间），供列表展示
- conversation_messages：每轮问答一行，用户回复/AI回复/思考过程/单轮用时/消耗token/
  用户名称/执行时间/状态/报错信息/request_id 各占一列，用于审计追溯
每次会话变更由 server.py 调用 upsert 幂等写回，启动时 load_all 恢复全部历史会话。
"""
import json
from datetime import datetime

import pymysql

from agentTest.db.db_config import get_mysql_config

# 会话元信息表（列表展示用）
_SCHEMA_CONVERSATIONS = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id VARCHAR(64)  NOT NULL COMMENT '会话ID（uuid4.hex，对应前端一个对话）',
    topic_id        VARCHAR(64)  NOT NULL DEFAULT '' COMMENT 'topic标识（日志/状态用）',
    creator         VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '创建人（仅用于区分不同用户，测试用）',
    title_override  VARCHAR(255) NOT NULL DEFAULT '' COMMENT '用户手动重命名的标题，空串表示未重命名',
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
    deleted_at      DATETIME     NULL DEFAULT NULL COMMENT '软删除时间（NULL=未删除，标记后前端不可见但保留记录）',
    PRIMARY KEY (conversation_id),
    KEY idx_creator (creator)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会话元信息'
"""

# 每轮问答明细表：信息点独立字段 + 审计字段（用户名称/执行时间/状态/报错信息/request_id）
_SCHEMA_MESSAGES = """
CREATE TABLE IF NOT EXISTS conversation_messages (
    id                BIGINT       AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    conversation_id   VARCHAR(64)  NOT NULL COMMENT '会话ID',
    round_no          INT          NOT NULL COMMENT '轮次（一问一答=1轮，从1开始）',
    creator           VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '用户名称（审计，冗余自会话元信息）',
    user_message      MEDIUMTEXT   NOT NULL COMMENT '用户回复',
    assistant_message MEDIUMTEXT   NOT NULL COMMENT 'AI回复',
    thinking          MEDIUMTEXT   NOT NULL COMMENT '思考过程',
    thinking_seconds  INT          NOT NULL DEFAULT 0 COMMENT '该轮对话用时（秒）',
    llm_tokens        JSON         NOT NULL COMMENT '消耗token（input/output/cache_hit/cache_miss）',
    sql_text          MEDIUMTEXT   NOT NULL COMMENT '实际执行的SQL',
    request_id        VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '关联日志编号',
    status            VARCHAR(16)  NOT NULL DEFAULT '' COMMENT '本轮状态（success/chat/failed）',
    error_message     TEXT         NOT NULL COMMENT '报错信息（成功为空，失败存安全错误编号）',
    request_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '本轮请求时间（执行时间）',
    created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '落盘时间',
    UNIQUE KEY uk_conv_round (conversation_id, round_no),
    KEY idx_conv (conversation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='每轮问答明细（独立字段+审计信息）'
"""

# 明细表需要保证存在的扩展列（审计 + 上下文占用），用于幂等迁移老表
_EXTRA_COLUMNS = ("creator", "status", "error_message", "request_at")


def _get_connection():
    """获取 MySQL 连接（复用 .env 的元数据 MySQL 配置）"""
    cfg = get_mysql_config()
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset=cfg["charset"],
    )


def init_db():
    """建表（幂等）；兼容旧版单 JSON 列结构：空表时重建；明细表缺审计列时补列。"""
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'conversations'"
            )
            exists = cursor.fetchone()[0] > 0
            if exists:
                cursor.execute("SHOW COLUMNS FROM conversations")
                cols = {row[0] for row in cursor.fetchall()}
                legacy = "messages" in cols
                missing_deleted = "deleted_at" not in cols
                if legacy or missing_deleted:
                    cursor.execute("SELECT COUNT(*) FROM conversations")
                    if cursor.fetchone()[0] == 0:
                        # 空表（含旧版 messages 单列结构）：直接重建
                        cursor.execute("DROP TABLE conversations")
                    elif missing_deleted and not legacy:
                        # 非空且仅缺软删除标记列：增量补列
                        cursor.execute("ALTER TABLE conversations ADD COLUMN deleted_at DATETIME NULL DEFAULT NULL COMMENT '软删除时间（NULL=未删除）'")
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'conversation_messages'"
            )
            msg_exists = cursor.fetchone()[0] > 0
            if msg_exists:
                cursor.execute("SHOW COLUMNS FROM conversation_messages")
                msg_cols = {row[0] for row in cursor.fetchall()}
                # 清理历史遗留列：上下文占用改为实时从 Checkpoint 现算，不再落库（幂等）
                for _legacy in ("context_progress", "context_compacted"):
                    if _legacy in msg_cols:
                        cursor.execute("ALTER TABLE conversation_messages DROP COLUMN " + _legacy)
                missing = [c for c in _EXTRA_COLUMNS if c not in msg_cols]
                if missing:
                    cursor.execute("SELECT COUNT(*) FROM conversation_messages")
                    if cursor.fetchone()[0] == 0:
                        cursor.execute("DROP TABLE conversation_messages")
                    else:
                        _alter_add_audit_columns(cursor, missing)
            cursor.execute(_SCHEMA_CONVERSATIONS)
            cursor.execute(_SCHEMA_MESSAGES)
        conn.commit()
    finally:
        conn.close()


def _alter_add_audit_columns(cursor, missing):
    """非空明细表增量补充审计列（幂等，缺哪列补哪列）。"""
    definitions = {
        "creator": "ALTER TABLE conversation_messages ADD COLUMN creator VARCHAR(64) NOT NULL DEFAULT '' COMMENT '用户名称'",
        "status": "ALTER TABLE conversation_messages ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT '' COMMENT '本轮状态'",
        "error_message": "ALTER TABLE conversation_messages ADD COLUMN error_message TEXT NOT NULL COMMENT '报错信息'",
        "request_at": "ALTER TABLE conversation_messages ADD COLUMN request_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '本轮请求时间'",
    }
    for col in missing:
        cursor.execute(definitions[col])


def _pair_to_messages(row):
    """把一轮问答明细组装回 {role:user}/{role:assistant} 两条前端消息。"""
    (conversation_id, round_no, creator, user_message, assistant_message, thinking,
     thinking_seconds, llm_tokens, sql_text, request_id, status, error_message, request_at) = row
    try:
        tokens = json.loads(llm_tokens) if llm_tokens else {}
    except (ValueError, TypeError):
        tokens = {}
    if hasattr(request_at, "strftime"):
        request_at_str = request_at.strftime("%Y-%m-%d %H:%M:%S")
    else:
        request_at_str = str(request_at or "")
    return [
        {"role": "user", "content": user_message},
        {
            "role": "assistant",
            "content": assistant_message,
            "thinking": thinking or "",
            "thinking_seconds": thinking_seconds or 0,
            "llm_tokens": tokens,
            "sql": sql_text or "",
            "request_id": request_id or "",
            "status": status or "",
            "error_message": error_message or "",
            "request_at": request_at_str,
            "evaluator": None,
            "dialogue_id": 0,
        },
    ]


def load_all():
    """加载全部会话为 {conversation_id: session_dict}，供启动时填充内存缓存。"""
    result = {}
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT conversation_id, topic_id, creator, title_override "
                "FROM conversations WHERE deleted_at IS NULL ORDER BY updated_at DESC"
            )
            conv_rows = cursor.fetchall()
            cursor.execute(
                "SELECT conversation_id, round_no, creator, user_message, assistant_message, thinking, "
                "thinking_seconds, llm_tokens, sql_text, request_id, status, error_message, request_at "
                "FROM conversation_messages ORDER BY conversation_id, round_no"
            )
            msg_rows = cursor.fetchall()
    finally:
        conn.close()
    # 按会话归并消息，保证同一会话内按轮次有序
    msgs_by_conv = {}
    for row in msg_rows:
        msgs_by_conv.setdefault(row[0], []).extend(_pair_to_messages(row))
    for row in conv_rows:
        conversation_id, topic_id, creator, title_override = row
        result[conversation_id] = {
            "topic_id": topic_id or "",
            "creator": creator or "",
            "title_override": title_override or "",
            "messages": msgs_by_conv.get(conversation_id, []),
        }
    return result


def _as_dict(msg):
    """消息统一按 dict 取值（防御历史非 dict 结构）。"""
    return msg if isinstance(msg, dict) else {}


def upsert(conversation_id, session):
    """幂等写回：元信息 + 每轮问答一行（按 round_no upsert，created_at 由库保留）。"""
    topic_id = session.get("topic_id", "")
    creator = session.get("creator", "")
    title_override = session.get("title_override", "")
    messages = session.get("messages", [])
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO conversations (conversation_id, topic_id, creator, title_override) "
                "VALUES (%s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "topic_id = VALUES(topic_id), creator = VALUES(creator), "
                "title_override = VALUES(title_override)",
                (conversation_id, topic_id, creator, title_override),
            )
            # 按对拆行：一问一答为一轮，各信息点写入独立字段（含审计字段）
            for i in range(0, len(messages), 2):
                user_msg = _as_dict(messages[i])
                assistant_msg = _as_dict(messages[i + 1]) if i + 1 < len(messages) else {}
                round_no = i // 2 + 1
                request_at = assistant_msg.get("request_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute(
                    "INSERT INTO conversation_messages "
                    "(conversation_id, round_no, creator, user_message, assistant_message, thinking, "
                    "thinking_seconds, llm_tokens, sql_text, request_id, status, error_message, request_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE "
                    "creator = VALUES(creator), user_message = VALUES(user_message), "
                    "assistant_message = VALUES(assistant_message), thinking = VALUES(thinking), "
                    "thinking_seconds = VALUES(thinking_seconds), llm_tokens = VALUES(llm_tokens), "
                    "sql_text = VALUES(sql_text), request_id = VALUES(request_id), "
                    "status = VALUES(status), error_message = VALUES(error_message), "
                    "request_at = VALUES(request_at)",
                    (
                        conversation_id,
                        round_no,
                        creator,
                        user_msg.get("content", ""),
                        assistant_msg.get("content", ""),
                        assistant_msg.get("thinking", ""),
                        assistant_msg.get("thinking_seconds", 0),
                        json.dumps(assistant_msg.get("llm_tokens", {}), ensure_ascii=False),
                        assistant_msg.get("sql", ""),
                        assistant_msg.get("request_id", ""),
                        assistant_msg.get("status", ""),
                        assistant_msg.get("error_message", ""),
                        request_at,
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def soft_delete(conversation_id):
    """软删除会话：仅打 deleted_at 标记，MySQL 记录保留（前端不再显示，明细供审计）。"""
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE conversations SET deleted_at = NOW() WHERE conversation_id = %s",
                (conversation_id,),
            )
        conn.commit()
    finally:
        conn.close()


def delete(conversation_id):
    """物理删除会话记录（元信息 + 明细），仅用于彻底清理/测试。"""
    conn = _get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM conversation_messages WHERE conversation_id = %s", (conversation_id,))
            cursor.execute("DELETE FROM conversations WHERE conversation_id = %s", (conversation_id,))
        conn.commit()
    finally:
        conn.close()