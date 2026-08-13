# -*- coding: utf-8 -*-
# 简要注释：日志配置模块，统一控制结构化日志的记录范围与长度上限。
import os
import dotenv

dotenv.load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


# 简要注释：是否记录 LLM 调用（llm.call / llm.response），默认开启。
LOG_LLM_ENABLED = _env_bool("LOG_LLM_ENABLED", True)

# 简要注释：LLM prompt 与输出在日志中的最大长度；0 表示不截断（保留完整输入输出）。
LOG_LLM_MAX_LENGTH = _env_int("LOG_LLM_MAX_LENGTH", 0)

# 简要注释：state 快照中列表类字段（候选/解析证据）最多记录的条数。
LOG_STATE_TOP_N = _env_int("LOG_STATE_TOP_N", 3)
