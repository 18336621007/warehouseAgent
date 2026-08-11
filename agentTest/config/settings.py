# 简要注释：项目配置模块，负责统一读取环境变量配置。

import os
import dotenv

dotenv.load_dotenv()


# 简要注释：获取大模型 API Key。
def get_openai_api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "")


# 简要注释：获取大模型兼容接口 Base URL。
def get_openai_base_url() -> str:
    return os.getenv("OPENAI_BASE_URL", "")


# 简要注释：获取聊天模型名称。
def get_model_name() -> str:
    return os.getenv("MODEL_NAME", "")


# 简要注释：获取向量模型名称。
def get_embedding_model_name() -> str:
    return os.getenv("EMBEDDING_MODEL", "")

def get_embedding_api_key() -> str:
    """Embedding 专用 API Key（不填则回退到 OPENAI_API_KEY）"""
    return os.getenv("EMBEDDING_API_KEY", "") or get_openai_api_key()

def get_embedding_base_url() -> str:
    """Embedding 专用 Base URL（不填则回退到 OPENAI_BASE_URL）"""
    return os.getenv("EMBEDDING_BASE_URL", "") or get_openai_base_url()


# 简要注释：读取 MODEL_ENABLE_THINKING（true/false），未配置返回 None 表示不传该参数，保持模型默认。
def get_model_enable_thinking():
    value = os.getenv("MODEL_ENABLE_THINKING", "").strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    return None


# 简要注释：返回可透传给 ChatOpenAI 的 extra_body（当前仅 enable_thinking）。
def get_model_extra_body() -> dict:
    enable_thinking = get_model_enable_thinking()
    if enable_thinking is None:
        return None
    return {"enable_thinking": enable_thinking}
