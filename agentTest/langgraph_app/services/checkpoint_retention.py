# checkpoint_retention.py —— LangGraph Checkpoint 保留策略
# 背景：checkpoint 每轮对话都会追加，但代码里没有任何删除逻辑，长期运行会持续占用磁盘。
# 策略：按"对话最后一次活动时间"只保留最近 N 天，与日志的 backupCount=14 保持一致。
# 说明：活动时间从 checkpoint_id 解析得到（LangGraph 使用 UUIDv6，前 48 位是 100ns 时间戳），
#       不依赖 MySQL、不需要改表结构，也不依赖文件 mtime。
import datetime
import sqlite3
import threading
import time
from pathlib import Path

# UUIDv1/v6 时间基准：1582-10-15 00:00:00 UTC，单位 100 纳秒
_UUID_EPOCH_OFFSET_SECONDS = 12219292800

# 单次 DELETE 的线程数上限：SQLite 变量数有上限（默认 999），分批更稳妥
_DELETE_BATCH = 400

# 默认 checkpoint 库路径：agentTest/langgraph_app/cache/checkpoints.db
DEFAULT_CHECKPOINT_DB = str(Path(__file__).resolve().parents[1] / "cache" / "checkpoints.db")

# 按需清理的节流状态：扫描与 VACUUM 分开计时（VACUUM 会重写整库并短暂锁库，必须节制）
_THROTTLE_LOCK = threading.Lock()
_last_prune_at = 0.0
_last_vacuum_at = 0.0

# 默认节流参数：扫描 30 分钟一次；VACUUM 每天最多一次
DEFAULT_MIN_INTERVAL_SECONDS = 1800
DEFAULT_VACUUM_MIN_INTERVAL_SECONDS = 86400


def parse_checkpoint_time(checkpoint_id: str):
    """从 checkpoint_id 解析出时间（UTC，带时区）。

    UUIDv6 结构：time_high(32) - time_mid(16) - version+time_low(16) - ...
    低 12 位时间戳需要跳过 4 位的 version 半字节。
    解析失败返回 None（例如历史遗留的非 UUIDv6 数据），调用方应跳过不删。
    """
    if not checkpoint_id:
        return None
    hex_str = str(checkpoint_id).replace("-", "")
    if len(hex_str) != 32:
        return None
    try:
        time_high = int(hex_str[0:8], 16)     # 高 32 位
        time_mid = int(hex_str[8:12], 16)     # 中 16 位
        time_low = int(hex_str[13:16], 16)    # 低 12 位（跳过 version 半字节）
    except ValueError:
        return None
    timestamp_100ns = (time_high << 28) | (time_mid << 12) | time_low
    unix_seconds = timestamp_100ns / 1e7 - _UUID_EPOCH_OFFSET_SECONDS
    return datetime.datetime.fromtimestamp(unix_seconds, tz=datetime.timezone.utc)


def find_expired_threads(db_path, retention_days: int):
    """扫描 checkpoint 库，返回超期未活动的 [(thread_id, 最后活动时间)]。

    只读取，不修改；库不存在或表不存在时返回空列表。
    """
    path = Path(db_path)
    if not path.exists():
        return []
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=retention_days)
    expired = []
    try:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            rows = conn.execute(
                "SELECT thread_id, MAX(checkpoint_id) FROM checkpoints GROUP BY thread_id"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        # 读库失败不应影响服务启动
        return []
    for thread_id, max_checkpoint_id in rows:
        last_active = parse_checkpoint_time(max_checkpoint_id)
        if last_active is None:
            continue                      # 解析不了就保留，安全优先
        if last_active < cutoff:
            expired.append((thread_id, last_active))
    return sorted(expired, key=lambda item: item[1])


def prune_checkpoints(db_path, retention_days: int = 14, dry_run: bool = False, vacuum: bool = True) -> dict:
    """删除超过保留天数的对话及其 checkpoint，并按需回收磁盘空间。

    返回统计信息：{scanned, expired, deleted_writes, deleted_checkpoints, vacuumed}。
    dry_run=True 时只统计不删除；vacuum=False 时删除后不做 VACUUM（供高频调用路径使用）。
    """
    path = Path(db_path)
    result = {
        "db_path": str(path),
        "retention_days": retention_days,
        "scanned": 0,
        "expired": [],
        "deleted_writes": 0,
        "deleted_checkpoints": 0,
        "vacuumed": False,
    }
    if not path.exists():
        return result

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=retention_days)
    conn = sqlite3.connect(str(path), timeout=60)
    try:
        cursor = conn.cursor()
        rows = cursor.execute(
            "SELECT thread_id, MAX(checkpoint_id) FROM checkpoints GROUP BY thread_id"
        ).fetchall()
        result["scanned"] = len(rows)

        expired_ids = []
        for thread_id, max_checkpoint_id in rows:
            last_active = parse_checkpoint_time(max_checkpoint_id)
            if last_active is None:
                continue
            if last_active < cutoff:
                expired_ids.append(thread_id)
                result["expired"].append({
                    "thread_id": thread_id,
                    "last_active": last_active.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                })
        result["expired"].sort(key=lambda item: item["last_active"])

        if dry_run or not expired_ids:
            return result

        # 分批删除，避免 SQLite 变量数超限
        for start in range(0, len(expired_ids), _DELETE_BATCH):
            batch = expired_ids[start:start + _DELETE_BATCH]
            placeholders = ",".join("?" * len(batch))
            result["deleted_writes"] += cursor.execute(
                f"DELETE FROM writes WHERE thread_id IN ({placeholders})", batch
            ).rowcount
            result["deleted_checkpoints"] += cursor.execute(
                f"DELETE FROM checkpoints WHERE thread_id IN ({placeholders})", batch
            ).rowcount
        conn.commit()

        # VACUUM 真正回收文件空间（同时会把 -wal 合并回主库）；调用方可按需关闭
        if vacuum:
            try:
                cursor.execute("VACUUM")
                result["vacuumed"] = True
            except sqlite3.Error:
                pass
        return result
    finally:
        conn.close()


def maybe_prune_checkpoints(
    db_path: str = None,
    retention_days: int = 14,
    min_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
    vacuum_min_interval_seconds: int = DEFAULT_VACUUM_MIN_INTERVAL_SECONDS,
    force: bool = False,
) -> dict:
    """按需清理（带节流）：供"每次请求结束"这类高频调用点使用。

    - 距上次扫描不足 min_interval_seconds 时直接跳过，避免每个请求都扫表；
    - VACUUM 额外按 vacuum_min_interval_seconds 节流（重操作，不能每次做）；
    - force=True 时忽略节流，用于服务启动或手工调用。
    任何异常都吞掉，返回带 skipped/error 信息的统计字典，绝不影响主流程。
    """
    global _last_prune_at, _last_vacuum_at

    target = db_path or DEFAULT_CHECKPOINT_DB
    now = time.time()
    try:
        with _THROTTLE_LOCK:
            if not force and (now - _last_prune_at) < min_interval_seconds:
                return {"skipped": True, "reason": "throttled", "db_path": str(target)}
            do_vacuum = force or (now - _last_vacuum_at) >= vacuum_min_interval_seconds
        stats = prune_checkpoints(target, retention_days=retention_days, vacuum=do_vacuum)
        with _THROTTLE_LOCK:
            _last_prune_at = now
            if stats.get("vacuumed"):
                _last_vacuum_at = now
        stats["skipped"] = False
        return stats
    except Exception as error:
        # 清理是旁路能力，失败不能影响查询主流程
        return {"skipped": False, "error": str(error), "db_path": str(target)}
