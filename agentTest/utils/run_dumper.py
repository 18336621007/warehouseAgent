import json
from datetime import datetime, date
from pathlib import Path
from uuid import uuid4
from decimal import Decimal



def dump_run(query, trace, schema_context=None, schema_rag_context=None, plan=None, run_summary=None, answer=None):
    # 生成本轮运行唯一 id，便于后续排查问题
    run_id = uuid4().hex

    # 记录当前时间，方便后续按时间回看运行记录
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 运行记录目录，按需自动创建
    run_dir = Path(__file__).resolve().parent.parent / "logs" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 将 PlanStep 对象转换为可序列化字典
    serialized_plan = []
    for step in plan or []:
        if hasattr(step, "to_dict"):
            serialized_plan.append(step.to_dict())
        else:
            serialized_plan.append(step)

    # 组装最终落盘内容，保留 schema_context 和 schema_rag_context 便于回放分析
    payload = {
        "run_id": run_id,
        "created_at": created_at,
        "query": query,
        "schema_context": schema_context or {},
        "schema_rag_context": schema_rag_context or [],
        "current_plan": serialized_plan,
        "trace": trace or [],
        "run_summary": run_summary or {},
        "answer": answer
    }

    # 以 run_id 命名文件，避免覆盖历史记录
    output_file = run_dir / f"{run_id}.json"

    with output_file.open("w", encoding="utf-8") as file:
        safe_payload = to_jsonable(payload)
        json.dump(safe_payload, file, ensure_ascii=False, indent=2)

    return str(output_file)


def to_jsonable(value):
    # 将运行结果递归转换为可 JSON 序列化的基础类型
    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, dict):
        return {
            str(key): to_jsonable(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]

    return value
