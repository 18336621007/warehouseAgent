# cleanup_checkpoints.py —— 手工执行 checkpoint 保留策略清理
# 服务启动时会自动清理一次；本脚本用于不停服时手工执行或挂定时任务。
# 用法：
#   python -m agentTest.scripts.cleanup_checkpoints --dry-run
#   python -m agentTest.scripts.cleanup_checkpoints --days 14
import argparse
import os
from pathlib import Path

from agentTest.config.settings import get_checkpoint_retention_days
from agentTest.langgraph_app.services.checkpoint_retention import prune_checkpoints

# 默认 checkpoint 路径：agentTest/langgraph_app/cache/checkpoints.db
_DEFAULT_DB = str(
    Path(__file__).resolve().parents[1] / "langgraph_app" / "cache" / "checkpoints.db"
)


def main():
    parser = argparse.ArgumentParser(description="清理超期的 LangGraph checkpoint")
    parser.add_argument("--db", default=_DEFAULT_DB, help="checkpoint 数据库路径")
    parser.add_argument(
        "--days",
        type=int,
        default=get_checkpoint_retention_days(),
        help="保留天数，默认取 CHECKPOINT_RETENTION_DAYS（14）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将删除的对话，不实际删除")
    args = parser.parse_args()

    before = os.path.getsize(args.db) if os.path.exists(args.db) else 0
    stats = prune_checkpoints(args.db, retention_days=args.days, dry_run=args.dry_run)
    after = os.path.getsize(args.db) if os.path.exists(args.db) else 0

    print(f"库文件: {stats['db_path']}")
    print(f"保留天数: {stats['retention_days']}")
    print(f"扫描到对话数: {stats['scanned']}")
    print(f"超期对话数: {len(stats['expired'])}")
    for item in stats["expired"]:
        print(f"  - {item['thread_id']}  最后活动 {item['last_active']}")

    if args.dry_run:
        print("\n[dry-run] 未做任何删除；去掉 --dry-run 即执行清理")
        return

    print(f"\n删除 checkpoints 行: {stats['deleted_checkpoints']}")
    print(f"删除 writes 行:      {stats['deleted_writes']}")
    print(f"VACUUM 回收空间:     {stats['vacuumed']}")
    print(f"文件大小: {before / 1024 / 1024:.2f} MB -> {after / 1024 / 1024:.2f} MB")


if __name__ == "__main__":
    main()
