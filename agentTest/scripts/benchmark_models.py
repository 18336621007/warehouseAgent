# 模型测速脚本：对比不同模型 / 思考模式(enable_thinking)下的响应速度
# 用法示例：
#   python -m agentTest.scripts.benchmark_models --models qwen3-max-preview,qwen3.7-plus --mode both --rounds 3
import argparse
import time
from pathlib import Path

import dotenv

# 显式加载 agentTest/.env，避免依赖当前工作目录
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
dotenv.load_dotenv(_ENV_FILE)

from openai import OpenAI  # noqa: E402

from agentTest.config.settings import get_model_name, get_openai_api_key, get_openai_base_url  # noqa: E402

DEFAULT_PROMPT = "查询昨天新增订单最多的经销商的名称、状态和业务经理"


def build_client():
    """根据 .env 配置创建 OpenAI 兼容客户端。"""
    api_key = get_openai_api_key()
    base_url = get_openai_base_url()
    if not api_key or not base_url:
        raise RuntimeError("缺少 OPENAI_API_KEY / OPENAI_BASE_URL，请检查 agentTest/.env")
    return OpenAI(api_key=api_key, base_url=base_url)


def _first_token_time(chunk, start):
    """判断是否收到首个有效 token（含 thinking 内容），返回相对时间或 None。"""
    if not chunk.choices:
        return None
    delta = chunk.choices[0].delta
    content = getattr(delta, "content", None)
    reasoning = getattr(delta, "reasoning_content", None)
    if content or reasoning:
        return time.perf_counter() - start
    return None


def measure_once(client, model, messages, enable_thinking, max_tokens, temperature):
    """执行一次请求，返回耗时/字符数/token 数等指标。"""
    kwargs = dict(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        stream=True,
    )
    # 注意：部分模型不接受 enable_thinking 参数，会抛 400，由调用方捕获
    if enable_thinking is not None:
        kwargs["extra_body"] = {"enable_thinking": enable_thinking}

    start = time.perf_counter()
    first_at = None
    text_parts = []
    usage = None

    try:
        # 优先请求 usage，便于计算 tokens/s
        stream = client.chat.completions.create(**kwargs, stream_options={"include_usage": True})
    except Exception:
        # 部分兼容服务不支持 include_usage，回退普通流式
        stream = client.chat.completions.create(**kwargs)

    for chunk in stream:
        if first_at is None:
            first_at = _first_token_time(chunk, start)
        if chunk.choices and getattr(chunk.choices[0].delta, "content", None):
            text_parts.append(chunk.choices[0].delta.content)
        if getattr(chunk, "usage", None):
            usage = chunk.usage

    total = time.perf_counter() - start
    text = "".join(text_parts)
    return {
        "ttft": first_at if first_at is not None else total,
        "total": total,
        "chars": len(text),
        "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
        "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
    }


def mode_label(mode):
    """把 enable_thinking 取值转成可读标签。"""
    if mode is None:
        return "default"
    return "thinking" if mode else "no-thinking"


def run_benchmark(client, model, prompt, rounds, mode, max_tokens, temperature):
    """对单个模型跑完 warmup + 计分轮次，返回汇总结果。"""
    messages = [{"role": "user", "content": prompt}]
    modes = [None]
    if mode in ("thinking", "both"):
        modes.append(True)
    if mode in ("no-thinking", "both"):
        modes.append(False)

    results = []
    for enable_thinking in modes:
        label = mode_label(enable_thinking)
        try:
            # warmup：建立连接，不计分
            measure_once(client, model, messages, enable_thinking, max_tokens, temperature)
            samples = []
            for i in range(rounds):
                data = measure_once(client, model, messages, enable_thinking, max_tokens, temperature)
                samples.append(data)
                print(
                    f"  [{label}] round{i+1}: ttft={data['ttft']:.2f}s "
                    f"total={data['total']:.2f}s chars={data['chars']} "
                    f"tokens={data['completion_tokens']}"
                )
            results.append((label, samples))
        except Exception as exc:
            # 模型不支持该参数（如 thinking-only 模型拒绝 enable_thinking=false）时提示并跳过
            print(f"  [{label}] 失败: {type(exc).__name__}: {exc}")
    return results


def print_summary(results):
    """打印汇总表格。"""
    print()
    print("=" * 100)
    print(f"{'模型':<28}{'模式':<14}{'平均TTFT(s)':<12}{'平均总耗时(s)':<14}{'平均字符':<10}{'平均tokens':<12}{'tokens/s':<10}")
    print("-" * 100)
    for model, model_results in results:
        for label, samples in model_results:
            avg = {k: sum(s[k] for s in samples) / len(samples) for k in ("ttft", "total", "chars", "completion_tokens")}
            if avg["completion_tokens"]:
                speed = avg["completion_tokens"] / avg["total"]
            else:
                speed = float("nan")
            print(
                f"{model:<28}{label:<14}{avg['ttft']:<12.2f}{avg['total']:<14.2f}"
                f"{avg['chars']:<10.0f}{avg['completion_tokens']:<12.0f}{speed:<10.1f}"
            )
    print("=" * 100)


def main():
    parser = argparse.ArgumentParser(description="对比不同模型 / 思考模式下的响应速度")
    parser.add_argument("--models", default=get_model_name(), help="逗号分隔的模型列表，默认取 .env 的 MODEL_NAME")
    parser.add_argument("--mode", choices=["both", "thinking", "no-thinking", "default"], default="both",
                        help="both=同时测开/关思考(默认)；thinking=只测开启；no-thinking=只测关闭；default=不传该参数")
    parser.add_argument("--rounds", type=int, default=2, help="每组合计分轮数（之前会 warmup 一次），默认 2")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="测试用问题，默认内置示例")
    parser.add_argument("--prompt-file", default=None, help="从文件读取测试问题（优先级高于 --prompt）")
    parser.add_argument("--max-tokens", type=int, default=1024, help="输出上限，默认 1024")
    parser.add_argument("--temperature", type=float, default=0.0, help="采样温度，默认 0")
    args = parser.parse_args()

    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    else:
        prompt = args.prompt
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    print(f"测试问题: {prompt[:60]}{'...' if len(prompt) > 60 else ''}")
    print(f"模型列表: {models}  模式: {args.mode}  轮数: {args.rounds}")

    client = build_client()
    all_results = []
    for model in models:
        print(f"\n>>> 模型: {model}")
        model_results = run_benchmark(client, model, prompt, args.rounds, args.mode, args.max_tokens, args.temperature)
        all_results.append((model, model_results))

    print_summary(all_results)


if __name__ == "__main__":
    main()
