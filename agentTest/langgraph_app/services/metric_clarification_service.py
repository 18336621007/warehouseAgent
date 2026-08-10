# 指标口径澄清服务：固化候选编号，模型判断用户选择，程序白名单校验并维护跨轮状态。
import re
import uuid

from agentTest.langgraph_app.services.metric_ambiguity_validator import (
    MetricAmbiguityValidator,
)


class MetricClarificationService:
    """统一处理指标澄清的展示、选择校验和 AnalysisSpec 状态回写。"""

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
            "clarification_id": clarification_id or uuid.uuid4().hex,
            "mention": mention,
            "question": "",
            "options": options,
            "status": "open",
            "created_request_id": "",
            "last_active_request_id": "",
            "resolved_value": {},
        }

    @staticmethod
    def freeze_reranked_options(
        ambiguity: dict,
        selected_fields: list[str],
        candidates: list[dict],
    ) -> list[dict]:
        """为模型精选后的字段按程序确定性顺序生成冻结选项（编号 1..n），供展示与 pending 共用。

        编号必须在精选后由程序统一分配；跨轮复用由 pending 生命周期保证，
        防止用户回复编号与展示列表错位。option 携带 mention，保证落位到对应指标概念。
        """
        mention = str(ambiguity.get("mention") or "")
        by_field = {
            str(candidate.get("field") or ""): candidate
            for candidate in (candidates or [])
        }
        options = []
        for index, field in enumerate(selected_fields or [], start=1):
            candidate = by_field.get(str(field or ""))
            if candidate is None:
                continue
            table = str(candidate.get("table") or "")
            table_short = table.split(".")[-1] if "." in table else table
            options.append({
                "index": index,
                "mention": mention,
                "field": str(field),
                "table": table,
                "table_short": table_short,
                "meaning": MetricAmbiguityValidator._build_meaning(candidate),
                "comment": str(candidate.get("comment") or "")[:200],
            })
        return options

    @classmethod
    def validate_user_selection(cls, user_selection: dict, pending: dict) -> dict | None:
        """模型判断的白名单校验：field 必须命中 pending.options，clarification_id 对齐。"""
        if not pending or not user_selection:
            return None
        if str(pending.get("status") or "open") != "open":
            return None
        if not user_selection.get("selected"):
            return None
        field = str(user_selection.get("field") or "").strip()
        cid = str(user_selection.get("clarification_id") or "").strip()
        if not field:
            return None
        if cid and cid != str(pending.get("clarification_id") or ""):
            return None
        selected = next(
            (
                option
                for option in (pending.get("options") or [])
                if str(option.get("field") or "") == field
            ),
            None,
        )
        if selected is None:
            return None
        candidate = {
            "table": selected.get("table", ""),
            "field": selected.get("field", ""),
            "semantic_type": "measure",
            "comment": selected.get("comment", ""),
            "aliases": [selected.get("meaning", "")] if selected.get("meaning") else [],
        }
        return {
            "mention": pending.get("mention", ""),
            "concept_type": "metric",
            "status": "resolved",
            "selected_field": selected.get("field", ""),
            "selected_table": selected.get("table", ""),
            "resolution_source": "explicit_user",
            "clarification_id": pending.get("clarification_id", ""),
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
            updated_spec["pending_clarifications"] = []
        else:
            pending = cls.build_pending_clarification(result, clarification_id)
            # 已有同一概念的 open pending 时复用原候选（延迟澄清恢复：编号不随后续召回变化）
            mention = pending.get("mention", "")
            if mention:
                for existing in (current_spec or {}).get("pending_clarifications") or []:
                    if (
                        existing.get("status") == "open"
                        and existing.get("mention") == mention
                        and existing.get("options")
                    ):
                        pending = existing
                        break
            updated_spec["pending_clarifications"] = [pending] if pending else []
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
    def _extract_comment_value(comment: str, key: str) -> str:
        """从元数据 comment 中提取“原始备注/别名”等字段值。"""
        for line in str(comment or "").splitlines():
            if line.startswith(key + ":"):
                return line[len(key) + 1:].strip()
        return ""

    @staticmethod
    def _build_option_label(option: dict) -> str:
        """口径选项标签：优先原始备注（可信），无原始备注才回退系统推断别名（增强元数据可能有误，需标注来源）。"""
        comment = str(option.get("comment") or "")
        raw_comment = MetricClarificationService._extract_comment_value(comment, "原始备注")
        if raw_comment:
            return raw_comment[:80]
        aliases = MetricClarificationService._extract_comment_value(comment, "别名")
        if aliases:
            return f"系统推断别名：{aliases.split('、')[0].strip()[:60]}"
        return (option.get("meaning") or "").strip() or option.get("field", "")

    @staticmethod
    def build_candidate_facts(result, with_aliases: bool = False) -> str:
        """候选口径只读事实：编号 + 原始备注 + 字段 + 来源表，供模型引用。

        默认不含增强别名（增强元数据可能有误）；with_aliases=True 时才回退到
        “系统推断别名”，用于对用户展示的兜底场景。
        """
        options = result.clarification_options or []
        lines = []
        for option in options:
            index = option.get("index", "")
            field = option.get("field", "")
            table_short = option.get("table_short", "")
            table_suffix = f"，来源表：{table_short}" if table_short else ""
            raw_comment = MetricClarificationService._extract_comment_value(
                option.get("comment") or "", "原始备注"
            )
            if raw_comment:
                label = raw_comment[:80]
            elif with_aliases:
                label = MetricClarificationService._build_option_label(option)
            else:
                label = "（无备注）"
            lines.append(f"{index}. {label}（字段：{field}{table_suffix}）")
        return "候选口径（只读事实）：\n" + "\n".join(lines)

    @staticmethod
    def validate_field_references(reply: str, result) -> bool:
        """兜底校验：LLM 回复中出现的 snake_case 字段/表标识必须命中候选，防止编造口径。"""
        allowed = set()
        for option in result.clarification_options or []:
            if option.get("field"):
                allowed.add(str(option["field"]))
            if option.get("table_short"):
                allowed.add(str(option["table_short"]))
        suspects = set(re.findall(r"[a-z][a-z0-9_]{2,}", reply or ""))
        # 只校验含下划线的 snake_case 标识符，避免误伤普通英文词
        suspects = {token for token in suspects if "_" in token}
        return suspects.issubset(allowed)

    @staticmethod
    def build_clarification_explanation(result) -> str:
        """生成口径区别说明：基于候选的原始备注/别名/来源表，不编造口径。"""
        options = result.clarification_options or []
        mention = ""
        for ambiguity in result.ambiguities or []:
            mention = ambiguity.get("mention", "")
            if mention:
                break
        head = (
            f"“{mention}”存在多个口径，各口径区别如下："
            if mention else "存在多个口径，各口径区别如下："
        )
        lines = [head]
        for option in options:
            index = option.get("index", "")
            field = option.get("field", "")
            table_short = option.get("table_short", "")
            table_suffix = f"，来源表：{table_short}" if table_short else ""
            # 展示分层：优先原始备注，无原始备注才回退系统推断别名
            label = MetricClarificationService._build_option_label(option)
            lines.append(f"{index}. {label}（字段：{field}{table_suffix}）")
        lines.append("请回复编号选择您需要的口径。")
        return "\n".join(lines)

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
            table_short = option.get("table_short", "")
            table_suffix = f"，来源表：{table_short}" if table_short else ""
            # 与口径区别说明保持一致：原始备注优先，无备注才回退系统推断别名
            label = MetricClarificationService._build_option_label(option)
            lines.append(f"{index}. {label}（字段：{field}{table_suffix}）")
        lines.append("请回复编号、字段名或完整中文含义。")
        return "\n".join(lines)
