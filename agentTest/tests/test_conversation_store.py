# conversation_store 对话记录落盘（MySQL）单元测试
# 覆盖：两张表建表、每轮问答独立字段落库（用户回复/AI回复/思考过程/用时/token）、
#       创建/更新/删除回路、load_all 按轮次组装回 messages
# MySQL 不可达时自动跳过（集成测试，需要 .env 里的 MYSQL_* 配置）
import json
import uuid
import unittest

import pymysql

from web import conversation_store as cs
from agentTest.db.db_config import get_mysql_config


def _mysql_available():
    """检测元数据 MySQL 是否可达，不可达则跳过集成测试"""
    try:
        cfg = get_mysql_config()
        conn = pymysql.connect(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            charset=cfg["charset"],
        )
        conn.close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_mysql_available(), "MySQL 不可达，跳过集成测试")
class ConversationStoreTest(unittest.TestCase):
    """对话记录 MySQL 存储：独立字段落库/读取/更新/删除回路。"""

    def setUp(self):
        cs.init_db()
        # 使用唯一会话ID，避免与真实会话冲突，tearDown 时清理
        self._cid = "test_" + uuid.uuid4().hex

    def tearDown(self):
        cs.delete(self._cid)

    def _sample_session(self):
        return {
            "topic_id": "t1",
            "creator": "张三",
            "title_override": "",
            "messages": [
                {"role": "user", "content": "查询昨天的订单数"},
                {"role": "assistant", "content": "昨天新增订单 100 单", "thinking": "先命中语义层，再生成SQL",
                 "thinking_seconds": 42, "sql": "SELECT 1", "request_id": "req1",
                 "status": "success", "error_message": "", "request_at": "2026-09-21 10:00:00",
                 "llm_tokens": {"input_tokens": 100, "output_tokens": 50, "cache_hit": 10, "cache_miss": 90}},
            ],
        }

    def test_create_load_delete_roundtrip(self):
        cs.upsert(self._cid, self._sample_session())
        allc = cs.load_all()
        self.assertIn(self._cid, allc)
        conv = allc[self._cid]
        self.assertEqual(conv["creator"], "张三")
        self.assertEqual(conv["topic_id"], "t1")
        # 组装回 messages：一问一答两条
        self.assertEqual(len(conv["messages"]), 2)
        self.assertEqual(conv["messages"][0]["role"], "user")
        self.assertEqual(conv["messages"][0]["content"], "查询昨天的订单数")

        cs.delete(self._cid)
        self.assertNotIn(self._cid, cs.load_all())

    def test_separate_columns_stored(self):
        # 直接查明细表，确认各信息点写入独立字段而非混在 JSON
        cs.upsert(self._cid, self._sample_session())
        cfg = get_mysql_config()
        conn = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                               password=cfg["password"], database=cfg["database"], charset=cfg["charset"])
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT creator, user_message, assistant_message, thinking, thinking_seconds, llm_tokens, sql_text, request_id, status, error_message, request_at "
                    "FROM conversation_messages WHERE conversation_id = %s",
                    (self._cid,),
                )
                row = cursor.fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        creator, user_msg, assistant_msg, thinking, secs, tokens, sql_text, rid, status, err, req_at = row
        self.assertEqual(creator, "张三")
        self.assertEqual(user_msg, "查询昨天的订单数")
        self.assertEqual(assistant_msg, "昨天新增订单 100 单")
        self.assertEqual(thinking, "先命中语义层，再生成SQL")
        self.assertEqual(secs, 42)
        self.assertEqual(json.loads(tokens)["input_tokens"], 100)
        self.assertEqual(sql_text, "SELECT 1")
        self.assertEqual(rid, "req1")
        self.assertEqual(status, "success")
        self.assertEqual(err, "")
        self.assertEqual(req_at.strftime("%Y-%m-%d %H:%M:%S"), "2026-09-21 10:00:00")

    def test_soft_delete_marks_but_keeps_records(self):
        # 软删除：load_all 不再返回，但 MySQL 元信息打 deleted_at 标记、明细仍保留
        cs.upsert(self._cid, self._sample_session())
        cs.soft_delete(self._cid)
        self.assertNotIn(self._cid, cs.load_all())
        cfg = get_mysql_config()
        conn = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                               password=cfg["password"], database=cfg["database"], charset=cfg["charset"])
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT deleted_at FROM conversations WHERE conversation_id = %s",
                    (self._cid,),
                )
                deleted_at = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id = %s",
                    (self._cid,),
                )
                detail_rows = cursor.fetchone()[0]
        finally:
            conn.close()
        # 标记已打（非 NULL）且明细未删
        self.assertIsNotNone(deleted_at)
        self.assertEqual(detail_rows, 1)

    def test_failed_round_audit_record(self):
        # 失败轮：报错信息/执行时间/用户名称独立落库，AI回复为空
        cs.upsert(self._cid, {
            "topic_id": "t1", "creator": "李四", "title_override": "",
            "messages": [
                {"role": "user", "content": "查询超时了"},
                {"role": "assistant", "content": "", "thinking": "执行报错",
                 "thinking_seconds": 5, "llm_tokens": {}, "request_id": "req_err",
                 "status": "failed", "error_message": "QUERY_EXECUTION_FAILED:abc123",
                 "request_at": "2026-09-21 11:00:00"},
            ],
        })
        conv = cs.load_all()[self._cid]
        self.assertEqual(len(conv["messages"]), 2)
        self.assertEqual(conv["messages"][1]["content"], "")
        self.assertEqual(conv["messages"][1]["status"], "failed")
        self.assertEqual(conv["messages"][1]["error_message"], "QUERY_EXECUTION_FAILED:abc123")
        # 直接查表确认报错信息/用户名称独立列
        cfg = get_mysql_config()
        conn = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                               password=cfg["password"], database=cfg["database"], charset=cfg["charset"])
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT creator, status, error_message FROM conversation_messages WHERE conversation_id = %s",
                    (self._cid,),
                )
                row = cursor.fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("李四", "failed", "QUERY_EXECUTION_FAILED:abc123"))

    def test_multi_round_upsert(self):
        # 两轮问答：模拟真实系统累积消息后 upsert（完整列表幂等覆盖各轮）
        cs.upsert(self._cid, self._sample_session())
        second = {
            "topic_id": "t1", "creator": "张三", "title_override": "改名",
            "messages": self._sample_session()["messages"] + [
                {"role": "user", "content": "那各平台的呢"},
                {"role": "assistant", "content": "科斯特100/锂纳斯80", "thinking": "按平台分组",
                 "thinking_seconds": 15, "llm_tokens": {}, "request_id": "req2"},
            ],
        }
        cs.upsert(self._cid, second)
        conv = cs.load_all()[self._cid]
        self.assertEqual(len(conv["messages"]), 4)
        self.assertEqual(conv["title_override"], "改名")
        self.assertEqual(conv["messages"][2]["content"], "那各平台的呢")
        self.assertEqual(conv["messages"][3]["thinking_seconds"], 15)


if __name__ == "__main__":
    unittest.main()