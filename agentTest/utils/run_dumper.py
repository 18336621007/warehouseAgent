import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from decimal import Decimal
from datetime import datetime, date



def dump_run(query, schema_context, current_plan, trace, run_summary, answer=None):
    # 生成本轮运行唯一 id，便于后续排查问题
    run_id = uuid4().hex

    # 记录当前时间，方便后续按时间回看运行记录
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 运行记录目录，按需自动创建
    run_dir = Path(__file__).resolve().parent.parent / "logs" / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 将 PlanStep 对象转换为可序列化字典
    serialized_plan = []
    for step in current_plan or []:
        if hasattr(step, "to_dict"):
            serialized_plan.append(step.to_dict())
        else:
            serialized_plan.append(step)

    # 组装最终落盘内容
    payload = {
        "run_id": run_id,
        "created_at": created_at,
        "query": query,
        "schema_context": schema_context,
        "current_plan": serialized_plan,
        "trace": trace,
        "run_summary": run_summary,
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
        # 金额类字段建议转 float，便于后续查看
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