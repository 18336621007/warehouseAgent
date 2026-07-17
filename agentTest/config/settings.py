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