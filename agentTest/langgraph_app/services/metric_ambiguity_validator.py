# 指标歧义门禁服务：submit_query_plan → lock_query_plan 之间的程序级业务口径校验。
# 只对 measure 类型字段做指标歧义判断，维度候选单独处理。
# 候选必须来自真实元数据；历史案例和最高相似度不能产生解析证据。
import re
from dataclasses import dataclass, field

from agentTest.config.advisor import (
    EXAMPLE_FIELD_BOOST,
    MAX_AMBIGUITY_CANDIDATES,
    MIN_CANDIDATE_SCORE,
)
from agentTest.langgraph_app.runtime.graph_logger import log_metric_event


@dataclass
class ResolutionResult:
    """指标解析结果，供 Advisor Graph 决定放行或拦截。"""
    resolved: bool                      # 所有 metric_mention 是否都已解决
    resolutions: list[dict] = field(default_factory=list)  # 已解决记录
    ambiguities: list[dict] = field(default_factory=list)  # 未解决记录（含候选）
    clarification_options: list[dict] = field(default_factory=list)  # 程序生成澄清选项
    reason: str = ""                    # 放行/拦截原因


def _is_measure_candidate(candidate: dict) -> bool:
    """判断候选是否为度量字段：优先使用 semantic_type，兼容旧文本标记。"""
    semantic_type = str(candidate.get("semantic_type") or "").lower()
    comment = str(candidate.get("comment") or "")
    return semantic_type == "measure" or "【度量】" in comment


def _mention_matches_candidate(mention: str, candidate: dict) -> bool:
    """指标概念与候选的关联判断：字段名、注释或别名包含该概念词。"""
    text = (mention or "").strip().lower()
    if not text:
        return False
    field = str(candidate.get("field") or "").lower()
    comment = str(candidate.get("comment") or "").lower()
    aliases = " ".join(
        str(alias) for alias in (candidate.get("aliases") or [])
    ).lower()
    return text in field or text in comment or text in aliases


def _parse_selection_number(text: str):
    """解析纯编号选择（1/2/B），非纯编号返回 None。"""
    value = (text or "").strip().upper()
    if re.fullmatch(r"\d{1,2}", value):
        return int(value)
    if re.fullmatch(r"[A-Z]", value):
        return ord(value) - ord("A") + 1
    return None


class MetricAmbiguityValidator:
    """基于真实元数据候选和用户选择证据的指标歧义校验器。"""

    def __init__(self, column_vector_store=None, semantic_defaults: dict = None):
        # column_vector_store 用于按指标词检索真实字段候选（缺失时只使用显式传入候选）
        self._column_vector_store = column_vector_store
        # 正式语义默认配置，第13-14课接入，当前默认空
        self._semantic_defaults = semantic_defaults or {}

    # ── 主入口 ──────────────────────────────────────────────

    def validate(
        self,
        metric_mentions: list[str],
        current_user_input: str = "",
        messages: list = None,
        planner_candidates: list = None,
        advisor_candidates: list = None,
        previous_resolutions: list = None,
        target_tables: list = None,
        example_fields: list = None,
    ) -> ResolutionResult:
        """对每个指标概念重算解析证据，返回是否全部 resolved。"""
        mentions = [m for m in (metric_mentions or []) if m and m.strip()]
        if not mentions:
            return ResolutionResult(resolved=True, reason="无指标概念需要解析")

        previous_map = {
            resolution.get("mention", ""): resolution
            for resolution in (previous_resolutions or [])
            if isinstance(resolution, dict) and resolution.get("mention")
        }

        resolutions: list[dict] = []
        ambiguities: list[dict] = []
        clarification_options: list[dict] = []

        for mention in mentions:
            candidates = self._collect_candidates(
                mention,
                planner_candidates=planner_candidates,
                advisor_candidates=advisor_candidates,
                target_tables=target_tables,
                example_fields=example_fields,
            )
            previous = previous_map.get(mention)

            # 已解析记录且用户本轮未改选时保持 resolved
            if self._keep_previous_resolution(previous, current_user_input, candidates):
                resolutions.append(previous)
                log_metric_event(
                    "metric_resolution.completed",
                    mention=mention,
                    field=previous.get("selected_field", ""),
                    source=previous.get("resolution_source", ""),
                )
                continue

            matched = self._match_user_selection(current_user_input, candidates)
            if matched:
                resolutions.append(self._build_resolution(
                    mention, matched, "explicit_user",
                ))
                log_metric_event(
                    "metric_resolution.completed",
                    mention=mention,
                    field=matched.get("field", ""),
                    source="explicit_user",
                )
                continue

            if len(candidates) == 1:
                resolutions.append(self._build_resolution(
                    mention, candidates[0], "unique_metadata",
                ))
                log_metric_event(
                    "metric_resolution.completed",
                    mention=mention,
                    field=candidates[0].get("field", ""),
                    source="unique_metadata",
                )
                continue

            default = self._semantic_defaults.get(mention)
            if default:
                resolutions.append(self._build_resolution(
                    mention, default, "semantic_default",
                ))
                log_metric_event(
                    "metric_resolution.completed",
                    mention=mention,
                    field=default.get("field", ""),
                    source="semantic_default",
                )
                continue

            ambiguity = self._build_ambiguity(mention, candidates)
            ambiguities.append(ambiguity)
            log_metric_event(
                "metric_ambiguity.detected",
                mention=mention,
                candidate_count=len(candidates),
                candidates=[
                    {
                        "field": item.get("field", ""),
                        "table": item.get("table", ""),
                        "score": item.get("score", 0),
                    }
                    for item in candidates
                ],
            )
            if not clarification_options:
                # 每次只追问一个最关键指标
                clarification_options = self._build_clarification_options(ambiguity)

        if ambiguities:
            log_metric_event(
                "metric_resolution.user_required",
                mentions=[item.get("mention", "") for item in ambiguities],
                clarification_options=clarification_options,
            )
            return ResolutionResult(
                resolved=False,
                resolutions=resolutions,
                ambiguities=ambiguities,
                clarification_options=clarification_options,
                reason="存在未解析指标: "
                + ", ".join(item.get("mention", "") for item in ambiguities),
            )

        return ResolutionResult(
            resolved=True,
            resolutions=resolutions,
            reason="全部指标已解析: "
            + "; ".join(
                f"{item.get('mention')}->{item.get('selected_field')}"
                for item in resolutions
            ),
        )

    def verify_submitted_resolutions(
        self,
        submitted: list[dict],
        result: ResolutionResult,
    ) -> tuple[bool, str]:
        """交叉验证 LLM 提交的 concept_resolutions，程序重算结果为准。"""
        recomputed = {
            item.get("mention", ""): item
            for item in list(result.resolutions) + list(result.ambiguities)
        }
        for item in (submitted or []):
            mention = item.get("mention", "")
            entry = recomputed.get(mention)
            if not entry:
                continue
            if entry.get("status") != "resolved":
                return False, (
                    f"指标 [{mention}] 程序判定为未解决，"
                    "LLM 声称的解析证据被拒绝"
                )
            if item.get("field") != entry.get("selected_field"):
                return False, (
                    f"指标 [{mention}] 提交字段与程序重算不一致"
                )
        return True, ""

    def to_plan_resolutions(self, result: ResolutionResult) -> dict:
        """将已解决指标转换为 QueryPlan.concept_resolutions 可审计结构。"""
        plan_resolutions = {}
        for item in result.resolutions:
            if item.get("selected_field"):
                plan_resolutions[item["mention"]] = {
                    "field": item["selected_field"],
                    "table": item.get("selected_table", ""),
                    "source": item.get("resolution_source", "unknown"),
                }
        return plan_resolutions

    # ── 候选收集与匹配 ──────────────────────────────────────

    def _collect_candidates(
        self,
        mention: str,
        planner_candidates: list = None,
        advisor_candidates: list = None,
        target_tables: list = None,
        example_fields: list = None,
    ) -> list[dict]:
        """收集真实元数据候选：向量库检索 + Planner/Advisor 候选，去重、收敛并排序。"""
        raw_candidates: list[dict] = []

        if self._column_vector_store is not None:
            try:
                docs_with_scores = (
                    self._column_vector_store.similarity_search_with_score(
                        mention,
                        k=8,
                    )
                )
                for doc, distance in docs_with_scores:
                    metadata = doc.metadata or {}
                    raw_candidates.append({
                        "table": metadata.get("table", ""),
                        "field": metadata.get(
                            "column", metadata.get("field", "")
                        ),
                        "semantic_type": metadata.get("fields_type", ""),
                        "comment": (doc.page_content or ""),
                        "aliases": self._extract_aliases(doc.page_content or ""),
                        "score": float(round(1 - float(distance) / 2, 4)),
                    })
            except Exception:
                # 向量库异常不阻断门禁，继续使用显式候选
                pass

        # Planner 全局候选按指标概念词过滤，避免无关字段混入
        for candidate in planner_candidates or []:
            if not isinstance(candidate, dict) or not candidate.get("field"):
                continue
            if not _mention_matches_candidate(mention, candidate):
                continue
            raw_candidates.append({
                "table": candidate.get("table", ""),
                "field": candidate.get("field", ""),
                "semantic_type": candidate.get(
                    "semantic_type",
                    candidate.get("fields_type", ""),
                ),
                "comment": candidate.get("comment", ""),
                "aliases": list(candidate.get("aliases") or []),
                "score": float(candidate.get("score", 0) or 0),
            })

        # Advisor 字段检索候选为按问题语义召回的候选，同样视为该概念候选
        for candidate in advisor_candidates or []:
            if not isinstance(candidate, dict) or not candidate.get("field"):
                continue
            raw_candidates.append({
                "table": candidate.get("table", ""),
                "field": candidate.get("field", ""),
                "semantic_type": candidate.get(
                    "semantic_type",
                    candidate.get("fields_type", ""),
                ),
                "comment": candidate.get("comment", ""),
                "aliases": list(candidate.get("aliases") or []),
                "score": float(candidate.get("score", 0) or 0),
            })

        # 度量优先；若全部被过滤则保留全部（兼容旧元数据缺少 fields_type）
        measure_candidates = [
            candidate for candidate in raw_candidates
            if _is_measure_candidate(candidate)
        ]
        if measure_candidates:
            matched = measure_candidates
        else:
            matched = raw_candidates

        # 目标表优先 + 优秀案例加权 + 分数下限 + 数量上限收敛，只展示最相关的少量候选
        return self._rank_candidates(
            self._deduplicate_candidates(matched),
            target_tables=target_tables,
            example_fields=example_fields,
        )

    @staticmethod
    def _extract_aliases(page_content: str) -> list[str]:
        """从字段检索文本中解析“别名: xxx、yyy”。"""
        match = re.search(r"别名:\s*(.+)", page_content or "")
        if not match:
            return []
        return [
            alias.strip()
            for alias in match.group(1).split("、")
            if alias.strip()
        ]

    @staticmethod
    def _deduplicate_candidates(candidates: list[dict]) -> list[dict]:
        """按 table.field 去重，合并信息并保留最高排序分数。"""
        merged: dict[tuple, dict] = {}
        for candidate in candidates:
            key = (candidate.get("table", ""), candidate.get("field", ""))
            existing = merged.get(key)
            if existing is None:
                merged[key] = dict(candidate)
                continue
            existing["score"] = max(
                float(existing.get("score", 0) or 0),
                float(candidate.get("score", 0) or 0),
            )
            if not existing.get("comment"):
                existing["comment"] = candidate.get("comment", "")
            existing_aliases = set(existing.get("aliases") or [])
            existing_aliases.update(candidate.get("aliases") or [])
            existing["aliases"] = list(existing_aliases)
        result = list(merged.values())
        # 排序分数只用于候选排序，不产生任何解析证据
        result.sort(
            key=lambda item: float(item.get("score", 0) or 0),
            reverse=True,
        )
        return result

    @staticmethod
    def _rank_candidates(
        candidates: list[dict],
        target_tables: list = None,
        example_fields: list = None,
    ) -> list[dict]:
        """候选收敛：目标表优先、相似度下限过滤、数量上限截断。

        收敛不改变“多候选必须追问”语义：过滤后只剩 1 个但原始候选多于 1 个时，
        保留前 2 个继续追问，避免截断把歧义误判为唯一口径。
        """
        target_set = {
            str(item).strip().lower()
            for item in (target_tables or [])
            if str(item).strip()
        }
        if target_set:
            # 目标表候选视为最相关；非目标表候选仅在目标表无候选时兜底
            target_candidates = [
                candidate for candidate in candidates
                if str(candidate.get("table") or "").strip().lower() in target_set
            ]
            ranked = target_candidates if target_candidates else candidates
        else:
            ranked = list(candidates)

        # 相似度下限：低于阈值视为不相关；过滤为空时回退全量，兼容无分数旧元数据
        filtered = [
            candidate for candidate in ranked
            if float(candidate.get("score") or 0) >= MIN_CANDIDATE_SCORE
        ]
        if not filtered:
            filtered = ranked

        # 优秀案例命中字段排序加权：仅影响展示顺序，不产生解析证据
        example_set = {
            str(field).strip().lower()
            for field in (example_fields or [])
            if str(field).strip()
        }
        if example_set:
            def _effective_score(item: dict) -> float:
                # 优秀案例命中字段加排序权重，原始 score 保持不变
                base = float(item.get("score") or 0)
                if str(item.get("field") or "").lower() in example_set:
                    return base + EXAMPLE_FIELD_BOOST
                return base

            filtered = sorted(filtered, key=_effective_score, reverse=True)

        truncated = filtered[:MAX_AMBIGUITY_CANDIDATES]
        # 多候选不允许被收敛成单候选，避免绕过用户确认
        if len(ranked) >= 2 and len(truncated) < 2:
            truncated = ranked[:2]
        return truncated

    def _keep_previous_resolution(
        self,
        previous: dict,
        current_user_input: str,
        candidates: list[dict],
    ) -> bool:
        """已解析记录在用户本轮未改选时继续有效。"""
        if not previous or previous.get("status") != "resolved":
            return False
        selected_field = previous.get("selected_field", "")
        if not selected_field:
            return False
        if selected_field not in {
            candidate.get("field", "") for candidate in candidates
        }:
            return False
        matched = self._match_user_selection(current_user_input, candidates)
        if matched and matched.get("field") != selected_field:
            return False
        return True

    @staticmethod
    def _match_user_selection(
        user_input: str,
        candidates: list[dict],
    ) -> dict:
        """判断用户是否明确选择了某个候选，返回该候选或 None。

        优先级：字段名精确匹配 → 选项编号 → 唯一业务词/别名匹配。
        多个候选同时命中业务词时视为仍未选择，不产生 explicit_user 证据。
        """
        text = (user_input or "").strip()
        if not text or not candidates:
            return None

        for candidate in candidates:
            if text.lower() == str(candidate.get("field") or "").lower():
                return candidate

        sequence = _parse_selection_number(text)
        if sequence is not None and 1 <= sequence <= len(candidates):
            return candidates[sequence - 1]

        text_lower = text.lower()
        strong_matches = []
        weak_matches = []
        for candidate in candidates:
            field = str(candidate.get("field") or "").lower()
            comment = str(candidate.get("comment") or "").lower()
            aliases = [
                str(alias).lower()
                for alias in (candidate.get("aliases") or [])
            ]
            # 强匹配：用户整个输入词出现在候选字段名或注释中
            if text_lower in field or text_lower in comment:
                strong_matches.append(candidate)
            # 弱匹配：用户输入包含别名，如“B类新增订单”包含“新增订单”
            elif any(alias and alias in text_lower for alias in aliases):
                weak_matches.append(candidate)

        # 唯一强匹配才构成 explicit_user；强匹配为空时才允许唯一弱匹配兜底
        if len(strong_matches) == 1:
            return strong_matches[0]
        if not strong_matches and len(weak_matches) == 1:
            return weak_matches[0]
        return None

    # ── 结果构造 ────────────────────────────────────────────

    @staticmethod
    def _build_resolution(
        mention: str,
        candidate: dict,
        source: str,
    ) -> dict:
        return {
            "mention": mention,
            "concept_type": "metric",
            "status": "resolved",
            "selected_field": candidate.get("field", ""),
            "selected_table": candidate.get("table", ""),
            "resolution_source": source,
            "candidates": [candidate],
        }

    @staticmethod
    def _build_ambiguity(mention: str, candidates: list[dict]) -> dict:
        return {
            "mention": mention,
            "concept_type": "metric",
            "status": "ambiguous",
            "selected_field": "",
            "selected_table": "",
            "resolution_source": "",
            "candidates": candidates,
        }

    @staticmethod
    def _build_meaning(candidate: dict) -> str:
        """从候选提取一句简洁中文含义：短说明 > 原始备注 > 首个别名 > 注释截断。"""
        comment = str(candidate.get("comment") or "")
        # 简短纯说明（非元数据全文）直接使用，如“净增订单数”
        if comment and "字段:" not in comment and len(comment) <= 60:
            return comment
        for line in comment.splitlines():
            if line.startswith("原始备注:"):
                return line[len("原始备注:"):].strip()[:60]
        aliases = [
            str(alias) for alias in (candidate.get("aliases") or [])
            if str(alias).strip()
        ]
        if aliases:
            return aliases[0][:60]
        return comment[:60]

    @staticmethod
    def _build_clarification_options(ambiguity: dict) -> list[dict]:
        """为第一个未解析指标生成程序候选选项，只保留字段名与一句简洁中文含义。"""
        options = []
        for index, candidate in enumerate(
            ambiguity.get("candidates") or [], start=1
        ):
            table = str(candidate.get("table") or "")
            table_short = table.split(".")[-1] if "." in table else table
            options.append({
                "index": index,
                "field": candidate.get("field", ""),
                "table": table,
                "table_short": table_short,
                "meaning": MetricAmbiguityValidator._build_meaning(candidate),
                "comment": str(candidate.get("comment") or "")[:200],
            })
        return options
