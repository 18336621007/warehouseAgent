# -*- coding: utf-8 -*-
# chart_png.py —— 静态图表 PNG 渲染器（飞书 / 离线场景）
# 目标：把 make_chart 生成的规范 spec（{type, xField, yField, data}）渲染成 PNG bytes，
#       复用同一份真实落盘数据，保证与网页端图表口径一致；无浏览器依赖（matplotlib Agg 后端）。
import io
import json
import os
import re
import matplotlib
matplotlib.use("Agg")  # 无界面后端，避免在无 GUI 环境报错
import matplotlib.pyplot as plt
from matplotlib import font_manager

# 中文字体：先按常见字体文件路径探测（兼容 Windows / Linux / macOS），
# 命中后注册到 matplotlib 并缓存字体路径供 wordcloud 复用，降低中文渲染成方块的概率
_FONT_FILE_CANDIDATES = (
    ("Microsoft YaHei", (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyh.ttf")),
    ("SimHei", (r"C:\Windows\Fonts\simhei.ttf",)),
    ("PingFang SC", ("/System/Library/Fonts/PingFang.ttc",)),
    ("Noto Sans CJK SC", (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    )),
    ("WenQuanYi Zen Hei", (
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/wqy/wqy-zenhei.ttc",
    )),
)
_FONT_NAME_CANDIDATES = ("Microsoft YaHei", "SimHei", "PingFang SC", "Noto Sans CJK SC", "WenQuanYi Zen Hei")
_CJK_FONT_PATH = ""  # 已解析的中文字体文件路径（wordcloud 渲染时复用）


def _apply_cjk_font():
    """探测并应用支持中文的 matplotlib 字体，返回命中的字体文件路径。"""
    global _CJK_FONT_PATH
    for _family, _paths in _FONT_FILE_CANDIDATES:
        for _path in _paths:
            if os.path.exists(_path):
                try:
                    font_manager.fontManager.addfont(_path)
                    _prop = font_manager.FontProperties(fname=_path)
                    plt.rcParams["font.sans-serif"] = [_prop.get_name(), "DejaVu Sans"]
                    _CJK_FONT_PATH = _path
                    return _path
                except Exception:
                    continue
    for _f in _FONT_NAME_CANDIDATES:
        try:
            _found = font_manager.findfont(_f, fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [_f, "DejaVu Sans"]
            _CJK_FONT_PATH = _found
            return _found
        except Exception:
            continue
    return ""


_apply_cjk_font()
plt.rcParams["axes.unicode_minus"] = False  # 正常显示负号

# 饼图最大扇区数：超出部分合并为“其他”，避免标签重叠（与前端 ECharts 行为一致）
_MAX_PIE_SLICES = 8


def _to_float(v):
    """尽力转数值，失败返回 None。"""
    if v is None:
        return None
    try:
        return float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def _prepare_xy(data, x_field, y_fields, series_names=None):
    """从 data 提取 x 序列与各 y 数值序列，返回 (x_vals, series_list)。"""
    x_vals = []
    names = series_names or []
    series_list = [{"key": f, "name": (names[i] if i < len(names) and names[i] else f), "values": []} for i, f in enumerate(y_fields)]
    for d in data:
        x_vals.append(str(d.get(x_field, "")))
        for s in series_list:
            s["values"].append(_to_float(d.get(s["key"])))  # 用原始字段名取值，中文名仅用于展示
    return x_vals, series_list


def _draw_pie(ax, x_vals, values, title):
    """饼图：过滤非正值、合并小扇区为“其他”，返回 ax。"""
    items = [(str(x), v) for x, v in zip(x_vals, values) if v is not None and v > 0]
    items.sort(key=lambda kv: -kv[1])
    if len(items) > _MAX_PIE_SLICES:
        keep = items[:_MAX_PIE_SLICES - 1]
        rest_sum = sum(v for _, v in items[_MAX_PIE_SLICES - 1:])
        keep.append(("其他", rest_sum))
        items = keep
    labels = [k for k, _ in items]
    nums = [v for _, v in items]
    total = sum(nums) or 1
    # 占比很小的扇区只显示在文字说明里，避免标签挤出图外
    show_labels = [l if (n / total) >= 0.03 else "" for l, n in items]
    ax.pie(
        nums, labels=show_labels, autopct=lambda pct: (f"{pct:.1f}%" if pct >= 3 else ""),
        startangle=90, counterclock=False,
        textprops={"fontsize": 9},
    )
    ax.axis("equal")
    if title:
        ax.set_title(title, fontsize=12)
    # 饼图扇区上限备注：默认最多展示 N 个，其余合并为“其他”
    ax.text(0.5, -0.12, "备注：默认最多展示 {} 个扇区，其余合并为“其他”。".format(_MAX_PIE_SLICES),
            transform=ax.transAxes, ha="center", fontsize=8, color="#555555")


def _render_wordcloud(spec: dict, width: float = 9.0, height: float = 4.6) -> bytes:
    """词云（关键词云图）：字号大小代表频次，与前端 ECharts wordCloud 一致。"""
    word_field = str(spec.get("wordField") or "word")
    val_field = str(spec.get("valueField") or "value")
    data = spec.get("data") or []
    title = str(spec.get("title") or "")
    pairs = []
    for d in data:
        name = str(d.get(word_field, "")).strip()
        val = _to_float(d.get(val_field))
        if name and val is not None and val > 0:
            pairs.append((name, val))
    if not pairs:
        raise ValueError("词云 spec 未找到有效的关键词/数值。")
    pairs.sort(key=lambda kv: -kv[1])
    pairs = pairs[:120]
    freq = dict(pairs)
    import os
    from wordcloud import WordCloud
    font_path = _CJK_FONT_PATH or None  # 复用图表渲染解析到的中文字体，兼容 Linux / Windows
    wc = WordCloud(font_path=font_path, width=1200, height=520, background_color="white",
                   prefer_horizontal=0.9, collocations=False, max_words=len(pairs), random_state=42)
    wc.generate_from_frequencies(freq)
    fig, ax = plt.subplots(figsize=(width, height), dpi=150)
    ax.imshow(wc, interpolation="bilinear")
    ax.axis("off")
    if title:
        ax.set_title(title, fontsize=12)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def render_spec_png(spec: dict, width: float = 9.0, height: float = 4.6) -> bytes:
    """把规范图表 spec 渲染成 PNG bytes；spec 结构同 make_chart 输出。"""
    chart_type = str(spec.get("type") or "line")
    if chart_type == "wordcloud":
        return _render_wordcloud(spec, width, height)
    x_field = str(spec.get("xField") or "")
    y_field = spec.get("yField") or ""
    data = spec.get("data") or []
    title = str(spec.get("title") or "")
    y_fields = y_field if isinstance(y_field, list) else [str(y_field or "")]
    y_fields = [f for f in y_fields if f]
    # 图表标签由 LLM 决定（make_chart 传入的 x_name/y_name/series_names），不再做程序映射
    x_name = str(spec.get("xName") or "")
    y_name = str(spec.get("yName") or "")
    series_names = list(spec.get("seriesNames") or [])
    if not x_field or not y_fields or not data:
        raise ValueError("图表 spec 缺少 xField / yField / data，无法渲染。")

    x_vals, series_list = _prepare_xy(data, x_field, y_fields, series_names)
    fig, ax = plt.subplots(figsize=(width, height), dpi=150)
    if chart_type == "pie":
        _draw_pie(ax, x_vals, series_list[0]["values"], title)
    else:
        # 折线 / 面积 / 柱状：共用类别轴
        if chart_type in ("line", "area"):
            for s in series_list:
                ax.plot(x_vals, s["values"], marker="o", markersize=3, linewidth=1.6, label=s["name"])
            if chart_type == "area":
                ax.fill_between(range(len(x_vals)), [v if v is not None else 0 for v in series_list[0]["values"]], alpha=0.15)
        else:  # bar
            import numpy as np
            idx = np.arange(len(x_vals))
            n_series = len(series_list)
            bar_w = 0.8 / max(n_series, 1)
            for i, s in enumerate(series_list):
                offset = (i - (n_series - 1) / 2) * bar_w
                ax.bar(idx + offset, [v if v is not None else 0 for v in s["values"]],
                       width=bar_w * 0.9, label=s["name"])
            ax.set_xticks(idx)
            ax.set_xticklabels(x_vals)  # 条形/柱状图也需要显式挂上真实标签，避免显示成 0、1、2…
        # x 轴标签过多时旋转，避免重叠
        if len(x_vals) > 12:
            plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
        ax.set_xlabel(x_name, fontsize=10)
        ax.set_ylabel(y_name, fontsize=10)
        # 存在负值时在 y=0 画参考线，便于观测正负
        all_vals = [v for s in series_list for v in s["values"] if v is not None]
        if any(v < 0 for v in all_vals):
            ax.axhline(0, color="#F0C040", linewidth=1.0)
        if series_list and series_list[0]["name"] and len(series_list) > 1:
            ax.legend(fontsize=9)
        ax.grid(axis="y", linestyle="--", alpha=0.3)
        if title:
            ax.set_title(title, fontsize=12)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def extract_chart_spec_from_answer(answer: str) -> dict | None:
    """从回答文本里提取第一个 ```chart 代码块的 JSON spec；没有则返回 None。"""
    if not answer:
        return None
    m = re.search(r"```\s*chart\s*\n(.*?)```", answer, re.S)
    if not m:
        return None
    try:
        spec = json.loads(m.group(1).strip())
        return spec if isinstance(spec, dict) else None
    except Exception:
        return None
