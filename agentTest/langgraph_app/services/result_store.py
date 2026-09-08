# result_store.py —— 查询结果历史存储（Result History Store）
# 职责：每轮查询结果全量落盘（JSON 元数据 + CSV 全量行），按 日期/对话 两级分类，
#       维护会话级索引 _index.json，支持跨轮引用（round_no/result_id）与保留策略清理。
# 安全：落盘是旁路能力，任何写盘/读盘失败都不应阻断主查询流程，统一 try/except 吞掉。
import csv
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from agentTest.config.advisor import (
    RESULT_STORE_ENABLED,
    RESULT_STORE_DIR,
    RESULT_STORE_MAX_ROUNDS,
    RESULT_STORE_MAX_DAYS,
    RESULT_STORE_MAX_PREVIEW_ROWS,
)

# 项目根目录（result_store.py -> services -> langgraph_app -> agentTest -> 项目根）
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _store_root() -> Path:
    """结果存储目录根（绝对路径）。"""
    return _PROJECT_ROOT / RESULT_STORE_DIR


def _load_json(path: Path):
    """读 JSON，失败返回 None（不抛异常）。"""
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _dump_json(path: Path, data) -> bool:
    """写 JSON，失败返回 False（落盘不影响主流程）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return True
    except Exception:
        return False


def _dump_csv(path: Path, columns: list, rows: list) -> bool:
    """把全量行写成 CSV（紧凑、可直接交付导出，UTF-8-SIG 兼容 Excel）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for row in rows:
                if isinstance(row, dict):
                    writer.writerow([row.get(c, "") for c in columns])
                else:
                    writer.writerow(list(row))
        return True
    except Exception:
        return False


def _conversation_dir(conversation_id: str) -> Path:
    """定位某对话的存储目录：优先复用已有目录（按首查日期），否则按今天日期新建。"""
    root = _store_root()
    today = date.today().isoformat()
    if conversation_id:
        try:
            for day_dir in root.iterdir() if root.exists() else []:
                if day_dir.is_dir():
                    conv_dir = day_dir / conversation_id
                    if conv_dir.is_dir():
                        return conv_dir
        except Exception:
            pass
    return root / today / conversation_id


def _index_path(conversation_dir: Path) -> Path:
    return conversation_dir / "_index.json"


def _read_index(conversation_dir: Path) -> dict:
    index = _load_json(_index_path(conversation_dir))
    if not isinstance(index, dict) or not isinstance(index.get("results"), list):
        index = {"conversation_id": conversation_dir.name, "results": []}
    return index


def _extract_entity_keys(state, preview_rows: list) -> list:
    """从预览行提取实体键（首个维度/首列值），结果追问指代用。"""
    confirmed_plan = state.get("confirmed_plan") or {}
    dimensions = confirmed_plan.get("dimensions") or []
    field = dimensions[0] if dimensions else ""
    keys = []
    seen = set()
    for row in preview_rows:
        if not field:
            break
        key = row.get(field)
        if key is None or key in seen:
            continue
        seen.add(key)
        keys.append(str(key))
        if len(keys) >= 10:
            break
    return keys


def _remove_files(conv_dir: Path, *names):
    """逐个删除对话目录下的文件，失败静默。"""
    for name in names:
        if not name:
            continue
        try:
            target = conv_dir / name
            # 只允许删除对话目录内部文件，防止路径逃逸
            if target.exists() and target.is_file() and str(target.resolve()).startswith(str(conv_dir.resolve())):
                target.unlink()
        except Exception:
            pass


def _cleanup_old_days():
    """清理超过保留天数的日期目录（整目录删除）。"""
    try:
        root = _store_root()
        if not root.exists():
            return
        cutoff = date.today() - timedelta(days=RESULT_STORE_MAX_DAYS)
        for day_dir in root.iterdir():
            if not day_dir.is_dir():
                continue
            try:
                day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                # rmtree 会连同日期目录本身一起删除，无需再 rmdir
                shutil.rmtree(day_dir, ignore_errors=True)
    except Exception:
        pass


def save_query_result(state, sql_result) -> dict:
    """把一轮查询结果落盘：JSON 元数据 + CSV 全量行 + 更新索引 + 保留策略清理。

    返回 {result_id, round_no, result_file, full_csv}；未启用或失败返回 {}。
    """
    if not RESULT_STORE_ENABLED:
        return {}
    try:
        request_id = str(state.get("request_id") or "")
        if not request_id:
            return {}
        conversation_id = str(state.get("conversation_id") or "")
        sql_result = sql_result or {}
        columns = list(sql_result.get("columns") or [])
        rows = list(sql_result.get("rows") or [])
        row_count = int(sql_result.get("row_count") or len(rows))
        conv_dir = _conversation_dir(conversation_id)
        conv_dir.mkdir(parents=True, exist_ok=True)

        # 全量行始终写 CSV（紧凑、可交付），JSON 只放预览，避免大结果膨胀
        full_csv = f"{request_id}_full.csv"
        _dump_csv(conv_dir / full_csv, columns, rows)

        preview_rows = []
        for row in rows[:RESULT_STORE_MAX_PREVIEW_ROWS]:
            if isinstance(row, dict):
                preview_rows.append(row)
            else:
                preview_rows.append(dict(zip(columns, row)))

        index = _read_index(conv_dir)
        round_no = len(index["results"]) + 1
        result_id = f"{request_id}:result"
        confirmed_plan = state.get("confirmed_plan") or {}
        entry = {
            "result_id": result_id,
            "source_request_id": request_id,
            "round_no": round_no,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "effective_query": str(
                state.get("effective_query") or state.get("current_user_input") or ""
            ),
            "table": confirmed_plan.get("table", ""),
            "columns": columns,
            "row_count": row_count,
            "preview_rows": preview_rows,
            "entity_keys": _extract_entity_keys(state, preview_rows),
            "result_summary": f"共 {row_count} 行，列：{', '.join(columns[:10]) or '无'}",
            "file": f"{request_id}.json",
            "full_csv": full_csv,
        }
        _dump_json(conv_dir / f"{request_id}.json", entry)

        # 更新索引：追加 + 只保留最近 MAX_ROUNDS 轮，被挤出的结果文件一并删除
        index["results"].append(entry)
        overflow = index["results"][:-RESULT_STORE_MAX_ROUNDS] if RESULT_STORE_MAX_ROUNDS > 0 else []
        index["results"] = index["results"][-RESULT_STORE_MAX_ROUNDS:] if RESULT_STORE_MAX_ROUNDS > 0 else []
        _dump_json(_index_path(conv_dir), index)
        for old in overflow:
            _remove_files(conv_dir, old.get("file"), old.get("full_csv"))
        _cleanup_old_days()

        return {
            "result_id": result_id,
            "round_no": round_no,
            "result_file": str(conv_dir / f"{request_id}.json"),
            "full_csv": str(conv_dir / full_csv),
        }
    except Exception:
        return {}


def list_result_index(conversation_id: str, limit: int = 8) -> list:
    """返回某对话最近 N 轮结果的轻量索引（供 prompt 注入，只含摘要）。"""
    if not RESULT_STORE_ENABLED or not conversation_id:
        return []
    conv_dir = _conversation_dir(conversation_id)
    if not _index_path(conv_dir).exists():
        return []
    index = _read_index(conv_dir)
    results = index.get("results") or []
    out = []
    for entry in results[-limit:]:
        out.append({
            "round_no": entry.get("round_no"),
            "created_at": entry.get("created_at"),
            "effective_query": entry.get("effective_query"),
            "table": entry.get("table"),
            "columns": entry.get("columns"),
            "row_count": entry.get("row_count"),
            "preview_rows": entry.get("preview_rows"),
            "entity_keys": entry.get("entity_keys"),
            "result_id": entry.get("result_id"),
            "full_csv": entry.get("full_csv"),
        })
    return out


def resolve_result(conversation_id: str, ref: str):
    """按 round_no 或 result_id 解析到索引条目；解析不到返回 None。"""
    if not RESULT_STORE_ENABLED or not conversation_id:
        return None
    conv_dir = _conversation_dir(conversation_id)
    index = _read_index(conv_dir)
    ref = str(ref or "").strip().lower()
    entries = index.get("results") or []
    for entry in reversed(entries):
        if ref == str(entry.get("round_no")) or ref == str(entry.get("result_id") or "").lower():
            return entry
    if ref.isdigit():
        for entry in reversed(entries):
            if str(entry.get("round_no")) == ref:
                return entry
    return None


def read_result_full(conversation_id: str, ref: str):
    """读取某轮结果全量：rows 从 CSV 读回，返回 {entry, rows}；解析不到返回 None。"""
    entry = resolve_result(conversation_id, ref)
    if not entry:
        return None
    conv_dir = _conversation_dir(conversation_id)
    csv_name = entry.get("full_csv") or f"{entry.get('source_request_id')}_full.csv"
    rows = []
    try:
        csv_path = conv_dir / csv_name
        if csv_path.exists():
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
    except Exception:
        rows = []
    return {"entry": entry, "rows": rows}
