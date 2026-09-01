# 指标口径澄清服务：程序只保证候选字段准确，模型判断用户选择，程序白名单校验并维护跨轮状态。
import re

from agentTest.langgraph_app.services.metric_ambiguity_validator import (
    MetricAmbiguityValidator,
)


def _find_field_semantic_type(
    field: str,
    candidate_objects: list[dict] = None,
    matched_group: dict = None,
) -> str:
    """从真实元数据候选中反查字段的语义类型（measure/dimension），查不到返回空串。"""
    for candidate in (candidate_objects or []):
        if str(candidate.get("field") or "") == field:
            semantic_type = str(
                candidate.get("semantic_type") or candidate.get("fields_type") or ""
            ).lower()
            if semantic_type:
                return semantic_type
    if matched_group:
        for candidate in (matched_group.get("candidates") or []):
            if str(candidate.get("field") or "") == field:
                semantic_type = str(
                    candidate.get("semantic_type") or candidate.get("fields_type") or ""
                ).lower()
                if semantic_type:
                    return semantic_type
    return ""


class MetricClarificationService:
    """统一处理指标澄清的展示、选择校验和 AnalysisSpec 状态回写。"""

    @staticmethod
    def build_shown_options(
        ambiguity: dict,
        selected_fields: list[str],
        candidates: list[dict],
    ) -> list[dict]:
        """为模型精选后的字段生成当轮展示选项（编号 1..n）。

        编号仅供当轮展示与兜底模板使用，不跨轮固化、不参与用户选择解析；
        用户按编号回复后，由模型结合对话历史中的展示文案还原对应字段。
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
            option = {
                "index": index,
                "mention": mention,
                "field": str(field),
                "table": table,
                "table_short": table_short,
                "meaning": MetricAmbiguityValidator._build_meaning(candidate),
                "comment": str(candidate.get("comment") or "")[:200],
            }
            # 过滤型候选：保留口径过滤信息，供展示/解析识别
            if str(candidate.get("semantic_type") or "").lower() == "filter":
                option["semantic_type"] = "filter"
                for key in (
                    "metric_id", "metric_name",
                    "filter_field", "filter_value",
                    "filter_label", "filter_model",
                ):
                    if candidate.get(key):
                        option[key] = candidate[key]
            options.append(option)
        return options

    @classmethod
    def validate_user_selection(
        cls,
        user_selection: dict,
        recent_shown_candidates: list[dict] = None,
        existing_resolutions: list[dict] = None,
        candidate_fields: list[str] = None,
        metric_mentions: list[str] = None,
        dimension_mentions: list[str] = None,
        candidate_objects: list[dict] = None,
    ) -> dict | None:
        """模型判断用户选择的白名单校验：field 必须命中候选集合
        （最近展示候选 ∪ 上轮已确认字段 ∪ 本轮召回字段），程序不解析用户文本。

        用户改选/补充选择时同样生效；字段不在任何候选集合时返回 None（宁可不解析，不猜测）。
        字段反查不到业务概念时同样返回 None，禁止把维度/属性字段硬挂到唯一指标概念下；
        编号到字段、字段到概念的映射由模型结合对话历史完成，程序只校验字段与概念的真实性。
        """
        if not user_selection:
            return None
        if not user_selection.get("selected"):
            return None
        field = str(user_selection.get("field") or "").strip()
        if not field:
            return None
        # 兼容 db.table.field 完整路径：统一拆成裸字段名后再做白名单匹配
        if "." in field:
            field = field.rsplit(".", 1)[-1]

        # 候选白名单 = 最近展示候选 ∪ 上轮已确认字段 ∪ 本轮召回字段
        allowed = set(str(item) for item in (candidate_fields or []) if item)
        matched_group = None
        for group in (recent_shown_candidates or []):
            for candidate in (group.get("candidates") or []):
                candidate_field = str(candidate.get("field") or "")
                if candidate_field:
                    allowed.add(candidate_field)
                if candidate_field == field:
                    matched_group = group
        for resolution in (existing_resolutions or []):
            selected_field = str(resolution.get("selected_field") or "")
            if selected_field:
                allowed.add(selected_field)
        if field not in allowed:
            return None

        # 反查业务概念与类型：优先最近展示候选，其次上轮已确认字段所属概念，
        # 最后使用模型自带 mention（必须命中指标/维度概念集合），反查不到不猜测
        mention = ""
        concept_type = ""
        selected = None
        if matched_group is not None:
            mention = str(matched_group.get("mention") or "")
            concept_type = str(matched_group.get("concept_type") or "")
            selected = next(
                (
                    candidate
                    for candidate in (matched_group.get("candidates") or [])
                    if str(candidate.get("field") or "") == field
                ),
                None,
            )
        else:
            for resolution in (existing_resolutions or []):
                if str(resolution.get("selected_field") or "") == field:
                    mention = str(resolution.get("mention") or "")
                    concept_type = str(resolution.get("concept_type") or "")
                    selected = resolution
                    break
        if not mention:
            # 模型自带概念归属：mention 必须命中指标或维度概念集合，类型按来源确定
            user_mention = str(user_selection.get("mention") or "").strip()
            if user_mention in (metric_mentions or []):
                mention, concept_type = user_mention, "metric"
            elif user_mention in (dimension_mentions or []):
                mention, concept_type = user_mention, "dimension"
        if not mention:
            # 字段在候选集合但无法反查概念：宁可不解析，交由 Advisor 继续澄清
            return None

        # 类型一致性校验：指标概念不允许解析到元数据为 dimension 的字段，
        # 防止“负责人”这类属性字段被当作指标口径收敛进 measures
        semantic_type = _find_field_semantic_type(
            field,
            candidate_objects=candidate_objects,
            matched_group=matched_group,
        )
        if concept_type == "metric" and semantic_type == "dimension":
            return None

        # 候选快照：保留展示过的全部候选（供后续改选参照），至少包含选中的字段
        snapshot_candidates = []
        if matched_group is not None:
            for candidate in (matched_group.get("candidates") or []):
                snapshot_candidates.append(dict(candidate))
        if not snapshot_candidates and isinstance(selected, dict):
            snapshot_candidates = list(selected.get("candidates") or [])
        if not snapshot_candidates:
            snapshot_candidates.append({
                "table": (selected or {}).get("table", ""),
                "field": field,
                "semantic_type": "dimension" if concept_type == "dimension" else "measure",
                "comment": (selected or {}).get("comment", ""),
                "aliases": [],
            })
        return {
            "mention": mention,
            "concept_type": concept_type or "metric",
            "status": "resolved",
            "selected_field": field,
            "selected_table": snapshot_candidates[0].get("table", ""),
            "resolution_source": "explicit_user",
            "candidates": snapshot_candidates,
        }

    @classmethod
    def update_recent_shown_candidates(cls, current_spec: dict, result) -> dict:
        """将 Validator 结果写回 AnalysisSpec：指标/维度解析证据分别落盘，
        未解决概念保留最近展示候选（无编号，带 concept_type），
        已解决时清空快照，避免过期候选误导改选判断。
        """
        updated_spec = dict(current_spec or {})
        # llm_submitted 只是模型单轮解读，不写入持久化状态，
        # 避免改选后旧口径在下一轮被当作“已确认”复活
        kept_items = [
            item for item in (
                list(result.resolutions or []) + list(result.ambiguities or [])
            )
            if item.get("resolution_source") != "llm_submitted"
        ]
        updated_spec["metric_resolutions"] = [
            item for item in kept_items
            if item.get("concept_type") != "dimension"
        ]
        updated_spec["dimension_resolutions"] = [
            item for item in kept_items
            if item.get("concept_type") == "dimension"
        ]
        if result.resolved:
            updated_spec["recent_shown_candidates"] = []
        else:
            shown = []
            for ambiguity in result.ambiguities or []:
                mention = str(ambiguity.get("mention") or "")
                candidates = ambiguity.get("candidates") or []
                if mention and candidates:
                    shown.append({
                        "mention": mention,
                        "concept_type": ambiguity.get("concept_type") or "metric",
                        "candidates": [dict(c) for c in candidates],
                    })
            updated_spec["recent_shown_candidates"] = shown
        return updated_spec
    @staticmethod
    def build_resolution_context(analysis_spec: dict) -> str:
        """构造已确认指标/维度上下文，供 Planner 判断用户选择/改选。

        只展示用户明确选择(explicit_user)或元数据唯一(unique_metadata)的解析；
        llm_submitted 只是模型单轮解读，不当作已确认口径展示，避免误导改选判断。
        """
        lines = []
        resolutions = list((analysis_spec or {}).get("metric_resolutions") or []) + list(
            (analysis_spec or {}).get("dimension_resolutions") or []
        )
        for resolution in resolutions:
            if resolution.get("status") != "resolved":
                continue
            if resolution.get("resolution_source") not in (
                "explicit_user",
                "unique_metadata",
            ):
                continue
            mention = resolution.get("mention", "")
            field = resolution.get("selected_field", "")
            table = resolution.get("selected_table", "")
            concept_type = resolution.get("concept_type") or "metric"
            if mention and field:
                field_path = f"{table}.{field}" if table else field
                lines.append(f"- {mention} -> {field_path}（{concept_type}）")
        if not lines:
            return ""
        return "\n".join([
            "【程序已确认指标/维度口径】",
            *lines,
            "这些字段来自用户明确选择，提交方案时必须直接使用，不得再次追问或改写。",
            "metric_mentions/dimension_mentions 必须逐字沿用上方概念字符串（如“新增订单”），"
            "禁止改写成候选展示含义（如“新增订单数”），改写会导致已确认口径断链并触发重复澄清。",
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
            if str(option.get("semantic_type") or "").lower() == "filter":
                # 过滤型候选：label 用"指标（过滤标签）+ 过滤条件"口径文案
                label = MetricClarificationService._build_option_label(option)
            elif raw_comment:
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
            if str(option.get("semantic_type") or "").lower() == "filter":
                # 过滤型候选：label 已含"按 xx=xx 过滤"口径，不再暴露合成字段标识
                lines.append(f"{index}. {label}{table_suffix}")
            else:
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
            if str(option.get("semantic_type") or "").lower() == "filter":
                # 过滤型候选：label 已含"按 xx=xx 过滤"口径，不再暴露合成字段标识
                lines.append(f"{index}. {label}{table_suffix}")
            else:
                lines.append(f"{index}. {label}（字段：{field}{table_suffix}）")
        lines.append("请回复编号、字段名或完整中文含义。")
        return "\n".join(lines)
