# 模型受限重排服务：程序召回真实候选后，由模型按用户意图挑选“最相关的 <=N 个”用于展示。
# 输入候选只含字段名 + 原始备注 + 来源表（不含增强别名）；输出字段必须命中候选集合（程序白名单校验）。
from typing import Callable

from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate

from agentTest.config.advisor import (
    MAX_AMBIGUITY_CANDIDATES,
    RERANK_MIN_CANDIDATES,
)
from agentTest.langgraph_app.prompts.reranker_prompt import RERANK_SYSTEM_PROMPT
from agentTest.langgraph_app.runtime.graph_logger import log_metric_event
from agentTest.langgraph_app.runtime.llm_log_handler import build_llm_logging_handler


class ClarificationRerankOutput(BaseModel):
    """模型对候选口径的精选结果。"""

    selected_fields: list[str] = Field(
        default_factory=list,
        description="从候选字段中挑选的最相关字段名，必须逐字等于候选集合中的 field，按相关度从高到低排序",
    )

    reasoning: str = Field(
        default="",
        description="挑选依据，引用用户原话与候选的原始备注",
    )


def _extract_raw_comment(comment: str) -> str:
    """从元数据注释中提取“原始备注”，无原始备注返回空串。"""
    for line in str(comment or "").splitlines():
        if line.startswith("原始备注:"):
            return line[len("原始备注:"):].strip()
    return ""


def build_rerank_input(
    mention: str,
    effective_query: str,
    history_text: str = "",
    user_input: str = "",
    candidates: list = None,
) -> str:
    """构造精选模型输入：候选只含字段名 + 原始备注 + 来源表，不含别名。"""
    candidates = candidates or []
    lines = [
        f"业务概念：{mention}",
        f"用户当前意图：{effective_query}",
    ]
    if user_input.strip():
        lines.append(f"用户本轮输入：{user_input}")
    if history_text.strip():
        lines.append(f"对话历史：\n{history_text}")
    lines.append("候选口径：")
    for index, candidate in enumerate(candidates, start=1):
        field = candidate.get("field", "")
        table = str(candidate.get("table") or "")
        table_short = table.split(".")[-1] if "." in table else table
        raw = _extract_raw_comment(candidate.get("comment") or "")
        label = raw or "（无备注）"
        lines.append(f"{index}. {label}（字段：{field}，来源表：{table_short}）")
    return "\n".join(lines)


def complete_selection(candidates: list[dict], selected_fields: list[str]) -> list[str]:
    """精选白名单校验 + 下限补足：越界字段剔除，不足 RERANK_MIN_CANDIDATES 时按程序排序补足。"""
    valid_fields: list[str] = []
    seen = set()
    for field in selected_fields or []:
        field = str(field or "").strip()
        if field and field not in seen and any(
            str(candidate.get("field") or "") == field
            for candidate in candidates
        ):
            seen.add(field)
            valid_fields.append(field)
    if len(valid_fields) < RERANK_MIN_CANDIDATES:
        for candidate in candidates:
            field = str(candidate.get("field") or "").strip()
            if field and field not in seen:
                seen.add(field)
                valid_fields.append(field)
            if len(valid_fields) >= RERANK_MIN_CANDIDATES:
                break
    return valid_fields[:MAX_AMBIGUITY_CANDIDATES]


def build_candidate_reranker(llm=None) -> Callable:
    """构建候选精选函数：rerank(mention, effective_query, history_text, user_input, candidates) -> (selected_fields, reasoning)。

    llm 缺失时按系统配置创建 ChatOpenAI；精选失败返回空列表，由调用方回退程序排序。
    """
    if llm is None:
        from langchain_openai import ChatOpenAI
        from agentTest.config.settings import (
            get_model_name,
            get_openai_api_key,
            get_openai_base_url,
            get_model_extra_body,
        )
        llm = ChatOpenAI(
            api_key=get_openai_api_key(),
            base_url=get_openai_base_url(),
            model=get_model_name(),
            temperature=0,
        extra_body=get_model_extra_body(),
            callbacks=[build_llm_logging_handler("candidate_reranker")],
        )
    structured_llm = llm.with_structured_output(ClarificationRerankOutput)
    prompt = ChatPromptTemplate.from_messages([
        ("system", RERANK_SYSTEM_PROMPT),
        ("human", "{input_text}"),
    ])
    chain = prompt | structured_llm

    def rerank(
        mention: str,
        effective_query: str,
        history_text: str = "",
        user_input: str = "",
        candidates: list = None,
    ) -> tuple[list[str], str]:
        candidates = candidates or []
        if not candidates:
            return [], ""
        input_text = build_rerank_input(
            mention,
            effective_query,
            history_text=history_text,
            user_input=user_input,
            candidates=candidates,
        )
        try:
            output = chain.invoke(
                {
                    "mention": mention,  # 显式传入本次澄清的指标概念，供 prompt 精排约束使用
                    "max_candidates": MAX_AMBIGUITY_CANDIDATES,
                    "input_text": input_text,
                },
                config={"metadata": {"caller": "candidate_reranker"}},
            )
        except Exception:
            # 精选失败不阻断澄清流程，回退程序排序
            return [], ""
        selected = complete_selection(candidates, output.selected_fields or [])
        log_metric_event(
            "candidate_rerank.selected",
            mention=mention,
            selected_fields=selected,
            reasoning=str(output.reasoning or ""),
        )
        return selected, str(output.reasoning or "")

    return rerank
