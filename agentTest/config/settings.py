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


# 简要注释：最终回答用快速模型名（LLM_FAST_MODEL，不填则回退 MODEL_NAME，仅关闭 thinking 加速格式化输出）。
def get_llm_fast_model() -> str:
    return os.getenv("LLM_FAST_MODEL", "").strip() or get_model_name()


# 简要注释：最终回答用快速模型 extra_body（默认关闭 thinking，只做结果格式化，不做深度推理）。
def get_llm_fast_extra_body() -> dict:
    return {"enable_thinking": False}


# 简要注释：是否启用 LLM 输出逐字流式（思考过程/最终回答），默认开启，可通过 .env 关闭回退非流式。
def get_stream_output_enabled() -> bool:
    return os.getenv("STREAM_OUTPUT_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


# 简要注释：结构化输出方式（json_mode=response_format json_object / json_schema / function_calling=强制工具调用），
# 不同模型/API 支持度不同（如 qwen thinking 不支持 tool_choice=required，用 json_mode），默认 json_mode 最通用。
def get_llm_structured_output_method() -> str:
    return os.getenv("LLM_STRUCTURED_OUTPUT_METHOD", "json_mode").strip().lower() or "json_mode"


# 简要注释：thinking 模式下是否允许 tool_choice=required/object（qwen thinking 不支持应设为 false），默认 false 更安全。
def get_llm_thinking_force_tool() -> bool:
    return os.getenv("LLM_THINKING_FORCE_TOOL", "false").strip().lower() in ("1", "true", "yes", "on")


# 简要注释：思考控制参数类型（enable_thinking / reasoning_effort / none），预留不同模型思考开关差异。
def get_llm_thinking_param() -> str:
    return os.getenv("LLM_THINKING_PARAM", "enable_thinking").strip().lower() or "enable_thinking"


# 简要注释：是否流式捕获并展示模型思考内容（reasoning_content），默认开启；思考内容只展示不拼入正式输出。
def get_llm_stream_reasoning() -> bool:
    return os.getenv("LLM_STREAM_REASONING", "true").strip().lower() in ("1", "true", "yes", "on")


# 简要注释：API 协议（chat_completions / responses），预留适配 OpenAI Responses 等不同协议。
def get_llm_wire_api() -> str:
    return os.getenv("LLM_WIRE_API", "chat_completions").strip().lower() or "chat_completions"


# 简要注释：技能索引披露预算（字符数，仿 Codex 渐进式披露：初始只给 name+description，选中才读全文）。
def get_skill_index_max_chars() -> int:
    return int(os.getenv("SKILL_INDEX_MAX_CHARS", "4000").strip() or "4000")
