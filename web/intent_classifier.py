# 意图识别模块 —— 用 LLM 区分闲聊和数据查询
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name
from agentTest.langgraph_app.runtime.llm_log_handler import build_llm_logging_handler


class IntentResult(BaseModel):
    intent: str = Field(description="chat / query")
    quick_reply: str = Field(default="", description="闲聊时给出友好回复")


INTENT_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是智能数仓助手，负责判断用户消息意图，并对闲聊给出简短友好的回复。

- chat：闲聊、问候、感谢、自我介绍等非数据查询。quick_reply 必须以“智能数仓助手”身份简短回应，例如用户问“你是谁”时回复“我是智能数仓助手，可以帮你查询数据仓库里的数据”。
- query：涉及统计、分析、筛选、对比等数据需求。quick_reply 留空。

模糊消息（如单独的"订单"）默认归为 chat。
返回 JSON：{{"intent":"chat/query","quick_reply":"闲聊回复或留空"}}"""),
    ("human", "{message}"),
])

_llm = None


def _get_llm():
    global _llm
    if _llm is None:
        _llm = ChatOpenAI(
            api_key=get_openai_api_key(),
            base_url=get_openai_base_url(),
            model=get_model_name(),
            temperature=0,
            timeout=10,
            callbacks=[build_llm_logging_handler("intent_classifier")],
        ).with_structured_output(IntentResult)
    return _llm


def classify_intent(message: str) -> IntentResult:
    return _get_llm().invoke(INTENT_PROMPT.invoke({"message": message}))