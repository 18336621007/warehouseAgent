# 指标口径澄清服务：固定上一轮候选编号，解析用户选择并维护跨轮状态。
import re


class MetricClarificationService:
    """统一处理指标澄清的展示、选择解析和 AnalysisSpec 状态回写。"""

    _CHINESE_NUMBERS = {
        "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }

    @classmethod
    def build_pending_clarification(cls, result, clarification_id: str = "") -> dict:
        """将本轮程序候选固化为待确认状态，后续按该顺序解释用户编号。"""
        options = [dict(option) for option in (result.clarification_options or [])]
        if not options:
            return {}
        mention = ""
        for ambiguity in result.ambiguities or []:
            if ambiguity.get("mention"):
                mention = ambiguity["mention"]
                break
        return {
            "clarification_id": clarification_id,
            "mention": mention,
            "options": options,
        }

    @classmethod
    def resolve_pending_selection(cls, current_user_input: str, pending: dict) -> dict | None:
        """仅依据上一轮固化候选解析编号、字段名或中文含义，不依赖业务硬编码。"""
        text = str(current_user_input or "").strip()
        options = list((pending or {}).get("options") or [])
        if not text or not options:
            return None

        selected = cls._match_by_index(text, options)
        if selected is None:
            selected = cls._match_by_exact_value(text, options)
        if selected is None:
            return None

        # 将用户选择转成标准解析记录，供 Validator 和 QueryPlan 继续复用。
        candidate = {
            "table": selected.get("table", ""),
            "field": selected.get("field", ""),
            "semantic_type": "measure",
            "comment": selected.get("comment", ""),
            "aliases": [selected.get("meaning", "")] if selected.get("meaning") else [],
        }
        return {
            "mention": (pending or {}).get("mention", ""),
            "concept_type": "metric",
            "status": "resolved",
            "selected_field": selected.get("field", ""),
            "selected_table": selected.get("table", ""),
            "resolution_source": "explicit_user",
            "candidates": [candidate],
        }

    @classmethod
    def update_analysis_spec(cls, current_spec: dict, result, clarification_id: str = "") -> dict:
        """将 Validator 结果写回 AnalysisSpec，并自动创建或清理待确认状态。"""
        updated_spec = dict(current_spec or {})
        updated_spec["metric_resolutions"] = (
            list(result.resolutions or []) + list(result.ambiguities or [])
        )
        if result.resolved:
            updated_spec.pop("pending_metric_clarification", None)
        else:
            pending = cls.build_pending_clarification(result, clarification_id)
            if pending:
                updated_spec["pending_metric_clarification"] = pending
        return updated_spec

    @staticmethod
    def build_resolution_context(analysis_spec: dict) -> str:
        """构造已确认指标上下文，确保 Advisor 使用程序状态而不是重新猜测。"""
        lines = []
        for resolution in (analysis_spec or {}).get("metric_resolutions") or []:
            if resolution.get("status") != "resolved":
                continue
            mention = resolution.get("mention", "")
            field = resolution.get("selected_field", "")
            table = resolution.get("selected_table", "")
            if mention and field:
                field_path = f"{table}.{field}" if table else field
                lines.append(f"- {mention} -> {field_path}")
        if not lines:
            return ""
        return "\n".join([
            "【程序已确认指标口径】",
            *lines,
            "这些字段来自用户明确选择，提交方案时必须直接使用，不得再次追问或改写。",
        ])

    @staticmethod
    def build_clarification_message(result) -> str:
        """根据程序候选构造稳定的用户澄清文本。"""
        options = result.clarification_options or []
        mention = ""
        for ambiguity in result.ambiguities or []:
            mention = ambiguity.get("mention", "")
            if mention:
                break
        head = (
            f"“{mention}”存在多个口径，请选择："
            if mention else "存在多个口径，请选择："
        )
        lines = [head]
        for option in options:
            index = option.get("index", "")
            field = option.get("field", "")
            meaning = option.get("meaning", "") or "暂无业务说明"
            table_short = option.get("table_short", "")
            table_suffix = f"，来源表：{table_short}" if table_short else ""
            lines.append(f"{index}. {meaning}（字段：{field}{table_suffix}）")
        lines.append("请回复编号、字段名或完整中文含义。")
        return "\n".join(lines)

    @classmethod
    def _match_by_index(cls, text: str, options: list[dict]) -> dict | None:
        """识别“2”“第二个”“选第二项”等通用序号表达。"""
        compact = re.sub(r"\s+", "", text)
        # 序号表达必须整体匹配，避免把字段名中的数字误判为候选编号。
        match = re.fullmatch(
            r"(?:好(?:的)?[,，]?)?(?:我)?(?:要|就)?(?:选择?|用|按)?"
            r"第?([0-9一二两三四五六七八九十]+)(?:个|项|条|种)?(?:口径)?(?:吧)?[。.!！]?",
            compact,
        )
        if not match:
            return None
        raw_index = match.group(1)
        index = int(raw_index) if raw_index.isdigit() else cls._CHINESE_NUMBERS.get(raw_index)
        if index is None:
            return None
        return next(
            (option for option in options if option.get("index") == index),
            None,
        )

    @staticmethod
    def _match_by_exact_value(text: str, options: list[dict]) -> dict | None:
        """识别精确字段名或中文含义，允许常见选择前缀。"""
        normalized = re.sub(r"^(我)?(选择|选|用|按)", "", text.strip(), count=1).strip(" ：:")
        normalized_lower = normalized.lower()
        for option in options:
            field = str(option.get("field") or "").strip()
            meaning = str(option.get("meaning") or "").strip()
            if field and normalized_lower == field.lower():
                return option
            if meaning and normalized == meaning:
                return option
        return None
