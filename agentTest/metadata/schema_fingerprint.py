# -*- coding: utf-8 -*-
"""schema_fingerprint.py — M2 字段/表级指纹计算与变更 diff

指纹只依赖 Hive 可观测的物理元数据（字段名/类型/注释 + 表注释），
不依赖 LLM 增强结果，保证“Hive 变了”才能触发重增强。
"""
import hashlib
import json


def column_fingerprint(name: str, type_: str = "", comment: str = "") -> str:
    """单个字段指纹：字段名 + 类型 + 原始注释"""
    raw = "|".join([name or "", type_ or "", comment or ""])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def table_fingerprint(columns: list, table_comment: str = "") -> tuple:
    """计算表级指纹与字段级指纹映射

    返回: (table_fingerprint, {字段名: 字段指纹})
    """
    col_fps = {
        col.get("name", ""): column_fingerprint(
            col.get("name", ""), col.get("type", ""), col.get("comment", "") or ""
        )
        for col in columns
    }
    raw = json.dumps(col_fps, ensure_ascii=False, sort_keys=True) + "|" + (table_comment or "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest(), col_fps


def diff_columns(prev_fps: dict, curr_fps: dict) -> tuple:
    """对比新旧字段指纹，返回 (added, modified, removed) 字段名列表"""
    added = [name for name in curr_fps if name not in prev_fps]
    removed = [name for name in prev_fps if name not in curr_fps]
    modified = [
        name for name in curr_fps
        if name in prev_fps and prev_fps[name] != curr_fps[name]
    ]
    return added, modified, removed
