


def validate_args(args, schema):
    required = schema.get("required", [])
    props = schema.get("properties", {})

    # 1. 检查必填字段
    for r in required:
        if r not in args:
            return False, f"缺少参数: {r}"

    # 2. 检查类型（简单版）
    for k, v in args.items():
        if k not in props:
            return False, f"未知参数: {k}"

        expected_type = props[k]["type"]

        if expected_type == "string" and not isinstance(v, str):
            return False, f"参数 {k} 类型错误，应为string"

    return True, "ok"
