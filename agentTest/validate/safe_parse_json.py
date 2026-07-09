import json
import re

def safe_parse_json(text: str):
    """文本解析为json，无法解析就返回空列表"""
    if not text:
        return []

    # 1️⃣ 去掉 markdown
    text = text.strip()
    text = text.replace("```json", "").replace("```", "")

    # 2️⃣ 提取 JSON 区域
    match = re.search(r"\[.*\]|\{.*\}", text, re.S)
    if not match:
        return []

    raw = match.group()

    # 3️⃣ 尝试解析
    try:
        return json.loads(raw)
    except:
        return []