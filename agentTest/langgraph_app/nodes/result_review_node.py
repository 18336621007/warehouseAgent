# result_review_node.py —— 历史查询结果回顾节点（result_follow_up 直达）
# 职责：用户引用历史查询结果（"给我完整的明细""第三轮那个""刚才的结果"）时，
#       直接读取 result_store 落盘的对应轮次结果全量，交给 LLM 生成回答，跳过重新查询。
# 安全：rows 全量只在本地 CSV/内存，prompt 最多注入 REVIEW_MAX_ROWS 行预览，防止 token 膨胀。
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import AIMessage
from agentTest.langgraph_app.runtime.graph_logger import elapsed_ms, log_node_end, log_node_start, log_node_error, start_timer
from agentTest.langgraph_app.runtime.llm_log_handler import build_llm_logging_handler
from agentTest.config.settings import get_openai_api_key, get_openai_base_url, get_model_name, get_model_extra_body
from agentTest.langgraph_app.services.result_store import read_result_full
from langchain_openai import ChatOpenAI

# 注入 LLM 的历史结果预览行数上限（超过则提示全量见 CSV 文件）
REVIEW_MAX_ROWS = 100

REVIEW_SYSTEM_PROMPT = "你是数据分析助手。用户引用了之前某一轮查询的结果，请严格基于该轮查询结果回答，不要编造信息。"
REVIEW_HUMAN_TEMPLATE = """用户问题：
{question}

【引用的历史查询结果（第{round_no}轮）】
查询需求：{effective_query}
列：{columns}
共 {row_count} 行
数据：
{result_text}

全量 CSV 文件：{full_csv}

请据此回答用户问题；若数据行数超过展示范围，说明全量已导出到上面的 CSV 文件路径。
"""


def _format_rows_for_llm(rows: list, columns: list, row_count: int) -> str:
    """把全量行转成 markdown 表格文本，截断到 REVIEW_MAX_ROWS 行避免 token 膨胀。"""
    if not rows:
        return "（无数据）"
    lines = []
    for row in rows[:REVIEW_MAX_ROWS]:
        cells = [str(row.get(c, "")) for c in columns]
        lines.append("| " + " | ".join(cells) + " |")
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    text = header + "\n" + sep + "\n" + "\n".join(lines)
    if row_count > len(rows[:REVIEW_MAX_ROWS]):
        text += f"\n……（共 {row_count} 行，仅展示前 {REVIEW_MAX_ROWS} 行，全量见 CSV）"
    return text


def build_result_review_node(runtime):
    """构建历史结果回顾节点：读取落盘结果并生成回答，直接结束本轮。"""
    chat_openai = ChatOpenAI(
        api_key=get_openai_api_key(),
        base_url=get_openai_base_url(),
        model=get_model_name(),
        temperature=0,
        extra_body=get_model_extra_body(),
        callbacks=[build_llm_logging_handler("result_review")],
    )
    prompt = ChatPromptTemplate.from_messages([
        ("system", REVIEW_SYSTEM_PROMPT),
        ("human", REVIEW_HUMAN_TEMPLATE),
    ])

    def result_review_node(state):
        timer = start_timer()
        log_node_start("result_review", question=str(state.get("current_user_input") or "")[:60])
        try:
            planner_entities = state.get("planner_entities") or {}
            ref = str(planner_entities.get("result_ref") or "")
            conversation_id = str(state.get("conversation_id") or "")
            data = read_result_full(conversation_id, ref)
            if not data:
                final_answer = (
                    "我无法定位到您引用的那轮查询结果，请确认是第几轮的结果（或换个说法），"
                    "或者直接描述您要查询的内容。"
                )
                log_node_end("result_review", branch="ref_not_found", ms=elapsed_ms(timer))
                return {
                    "final_answer": final_answer,
                    "topic_status": "completed",
                    "messages": [AIMessage(
                        content=final_answer,
                        name="seeker",
                        id=f"{state.get('request_id', '')}:seeker",
                    )],
                }

            entry = data["entry"]
            rows = data["rows"]
            columns = list(entry.get("columns") or [])
            row_count = int(entry.get("row_count") or len(rows))
            result_text = _format_rows_for_llm(rows, columns, row_count)
            full_csv = str(entry.get("full_csv") or "")
            prompt_value = prompt.invoke({
                "question": str(state.get("current_user_input") or ""),
                "round_no": entry.get("round_no", ""),
                "effective_query": entry.get("effective_query", ""),
                "columns": ", ".join(columns),
                "row_count": row_count,
                "result_text": result_text,
                "full_csv": full_csv,
            })
            final_answer = chat_openai.invoke(prompt_value)
            final_answer = str(getattr(final_answer, "content", final_answer))
            log_node_end("result_review", branch="success", rows=row_count, ms=elapsed_ms(timer))
            return {
                "final_answer": final_answer,
                "topic_status": "completed",
                "result_csv": full_csv,
                "messages": [AIMessage(
                    content=final_answer,
                    name="seeker",
                    id=f"{state.get('request_id', '')}:seeker",
                )],
            }
        except Exception as error:
            log_node_error("result_review", error=str(error), ms=elapsed_ms(timer))
            return {
                "final_answer": "很抱歉，回顾历史查询结果时出现异常，请稍后重试。",
                "topic_status": "failed",
            }

    return result_review_node
