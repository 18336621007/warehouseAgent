# ── state/base_state.py ──
# 所有子图共享的基础字段
from typing import TypedDict

class BaseState(TypedDict, total=False):
    question: str
    original_question: str       # 话题原始问题，新话题更新
    user_response: str           # 用户本轮实际输入