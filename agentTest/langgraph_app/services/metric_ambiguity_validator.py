# 指标歧义门禁服务：submit_query_plan → lock_query_plan 之间的程序级业务口径校验。
# 只对 measure 类型字段做指标歧义判断，维度候选单独处理。
# 候选必须来自真实元数据；历史案例和最高相似度不能产生解析证据。
# 用户回复由澄清服务优先按上一轮候选解析；Validator 只校验字段合法性。
import re
from dataclasses import dataclass, field

from agentTest.config.advisor import (
    EXAMPLE_FIELD_BOOST,
    MAX_AMBIGUITY_CANDIDATES,
    MIN_CANDIDATE_SCORE,
    RERANK_MIN_CANDIDATES,
)
from agentTest.config.planner import (
    COLUMN_SEARCH_K,
    TABLE_SEARCH_K,
    PER_TABLE_COLUMN_QUOTA,
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


def _is_dimension_candidate(candidate: dict) -> bool:
    """判断候选是否为维度字段：优先使用 semantic_type，兼容旧文本标记。"""
    semantic_type = str(candidate.get("semantic_type") or "").lower()
    comment = str(candidate.get("comment") or "")
    return semantic_type == "dimension" or "【维度】" in comment


def _is_filter_candidate(candidate: dict) -> bool:
    """判断候选是否为"指标+过滤"型（如 A类→company_category=A）。"""
    semantic_type = str(candidate.get("semantic_type") or "").lower()
    return semantic_type == "filter"


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
        planner_candidates: list = None,
        advisor_candidates: list = None,
        previous_resolutions: list = None,
        target_tables: list = None,
        example_fields: list = None,
        llm_resolutions: list = None,
        table_candidates: list = None,
        truncate: bool = True,
        dimension_mentions: list[str] = None,
    ) -> ResolutionResult:
        """对每个业务概念（指标/维度）计算解析结果：信任 LLM 提交的解析字段，程序只校验字段合法性。"""
        concepts: list[tuple[str, str]] = []
        for mention in (metric_mentions or []):
            if mention and mention.strip():
                concepts.append((mention.strip(), "metric"))
        for mention in (dimension_mentions or []):
            if mention and mention.strip() and mention.strip() not in {
                item[0] for item in concepts
            }:
                concepts.append((mention.strip(), "dimension"))
        if not concepts:
            return ResolutionResult(resolved=True, reason="无业务概念需要解析")

        previous_map = {
            resolution.get("mention", ""): resolution
            for resolution in (previous_resolutions or [])
            if isinstance(resolution, dict) and resolution.get("mention")
        }
        # LLM 提交的 concept_resolutions：对用户回复的解读完全信任，程序只校验字段合法性
        # 兼容 mention/user_intent/concept 等概念字段名
        llm_map = {}
        for item in (llm_resolutions or []):
            if not isinstance(item, dict):
                continue
            mention = str(
                item.get("mention") or item.get("user_intent") or item.get("concept") or ""
            ).strip()
            if mention:
                llm_map[mention] = item

        # 维度枚举值候选发现：指标概念额外生成"指标+过滤"型候选（如 A类→company_category=A）。
        # 只在存在指标概念时触发；范围收敛到指标 source_model + join 契约可达模型。
        filter_candidates: list[dict] = []
        if any(concept_type == "metric" for _, concept_type in concepts):
            from agentTest.semantic_layer.metric_matcher import match_metrics_from_query
            from agentTest.semantic_layer.semantic_layer_provider import (
                get_semantic_layer_provider,
            )
            mention_terms = [mention for mention, _ in concepts]
            matched_metrics = match_metrics_from_query(" ".join(mention_terms), limit=8)
            if matched_metrics:
                filter_candidates = (
                    get_semantic_layer_provider().discover_dimension_filter_candidates(
                        mention_terms,
                        matched_metrics,
                    )
                )

        resolutions: list[dict] = []
        ambiguities: list[dict] = []
        clarification_options: list[dict] = []

        for mention, concept_type in concepts:
            candidates = self._collect_candidates(
                mention,
                planner_candidates=planner_candidates,
                advisor_candidates=advisor_candidates,
                target_tables=target_tables,
                example_fields=example_fields,
                table_candidates=table_candidates,
                truncate=truncate,
                concept_type=concept_type,
            )
            # 指标概念把"指标+过滤"型候选并入候选池（过滤候选用合成 field 保证唯一，
            # 不参与 measure/dimension 分流，由 _build_clarification_options 特殊渲染）
            if concept_type == "metric" and filter_candidates:
                candidates = self._merge_filter_candidates(
                    candidates, filter_candidates,
                )
            # 维度概念无任何候选（字段召回与过滤候选均为空）时跳过，
            # 避免 0 候选 ambiguity 阻塞锁定；该修饰词语义由指标候选承载
            if concept_type == "dimension" and not candidates:
                log_metric_event(
                    "metric_resolution.skipped",
                    mention=mention,
                    reason="维度概念无候选",
                )
                continue
            previous = previous_map.get(mention)
            llm_resolution = llm_map.get(mention)

            # 信任 LLM 对用户回复的解读：字段必须落在真实候选或上轮已确认字段内
            llm_field = str(
                (llm_resolution or {}).get("field")
                or (llm_resolution or {}).get("resolved_field")
                or (llm_resolution or {}).get("selected_field")
                or ""
            ).strip()
            if llm_field:
                resolution = self._resolve_from_llm(
                    mention, llm_field, previous, candidates,
                    concept_type=concept_type,
                )
                if resolution is not None:
                    resolutions.append(resolution)
                    log_metric_event(
                        "metric_resolution.completed",
                        mention=mention,
                        field=resolution.get("selected_field", ""),
                        source=resolution.get("resolution_source", ""),
                    )
                    continue

            # 上轮已解决且字段仍有效时保持，避免重复确认
            if self._keep_previous_resolution(previous, candidates):
                resolutions.append(previous)
                log_metric_event(
                    "metric_resolution.completed",
                    mention=mention,
                    field=previous.get("selected_field", ""),
                    source=previous.get("resolution_source", ""),
                )
                continue

            if len(candidates) == 1:
                # 唯一候选直锁仅限与概念语义相关的字段：维度概念候选来自向量召回，
                # 若与 mention 无字面/别名关联（如“负责人”遇到 company_business_type），
                # 不直锁、继续澄清，避免把不相关维度字段当成唯一口径
                if concept_type == "dimension" and not _mention_matches_candidate(
                    mention, candidates[0]
                ):
                    ambiguity = self._build_ambiguity(
                        mention, candidates, concept_type=concept_type,
                    )
                    ambiguities.append(ambiguity)
                    if not clarification_options:
                        clarification_options = self._build_clarification_options(ambiguity)
                    continue
                resolutions.append(self._build_resolution(
                    mention, candidates[0], "unique_metadata",
                    concept_type=concept_type,
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

            ambiguity = self._build_ambiguity(
                mention, candidates, concept_type=concept_type,
            )
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

    def to_plan_resolutions(self, result: ResolutionResult) -> dict:
        """将已解决指标转换为 QueryPlan.concept_resolutions 可审计结构。"""
        plan_resolutions = {}
        for item in result.resolutions:
            if item.get("selected_field"):
                entry = {
                    "field": item["selected_field"],
                    "table": item.get("selected_table", ""),
                    "source": item.get("resolution_source", "unknown"),
                    "concept_type": item.get("concept_type") or "metric",
                }
                # 过滤型候选：保留口径过滤信息，供 Advisor 落 filters
                if item.get("semantic_type") == "filter":
                    entry["semantic_type"] = "filter"
                    for key in (
                        "metric_id", "metric_name",
                        "filter_field", "filter_value",
                        "filter_label", "filter_model",
                    ):
                        if item.get(key):
                            entry[key] = item[key]
                plan_resolutions[item["mention"]] = entry
        return plan_resolutions

    # ── 候选收集与匹配 ──────────────────────────────────────

    @staticmethod
    def _merge_filter_candidates(
        candidates: list[dict],
        filter_candidates: list[dict],
    ) -> list[dict]:
        """把"指标+过滤"候选并入字段候选池，按合成 field 去重。"""
        if not filter_candidates:
            return candidates
        seen = {str(c.get("field") or "") for c in candidates}
        merged = list(candidates)
        for fc in filter_candidates:
            field = str(fc.get("field") or "")
            if field and field not in seen:
                seen.add(field)
                merged.append(fc)
        return merged

    def _collect_candidates(
        self,
        mention: str,
        planner_candidates: list = None,
        advisor_candidates: list = None,
        target_tables: list = None,
        example_fields: list = None,
        table_candidates: list = None,
        truncate: bool = True,
        concept_type: str = "metric",
    ) -> list[dict]:
        """两段式召回真实元数据候选：先按目标表/表级候选确定表作用域（先召回表），
        再在作用域表内逐表检索字段，最后全局字段检索兜底（再召回字段）；
        再合并 Planner/Advisor 显式候选，去重、收敛并排序；
        metric 概念优先度量字段，dimension 概念优先维度字段。"""
        raw_candidates: list[dict] = []

        # 表作用域 = 目标表 + 表级召回 top-K（“先召回表”）
        scoped_tables = list(dict.fromkeys(
            str(item).strip().lower()
            for item in (target_tables or [])
            if str(item).strip()
        ))
        if table_candidates:
            ordered_tables = sorted(
                (
                    item for item in table_candidates
                    if isinstance(item, dict) and item.get("table")
                ),
                key=lambda item: float(item.get("score", 0) or 0),
                reverse=True,
            )
            for item in ordered_tables:
                table_name = str(item.get("table") or "").strip().lower()
                if table_name and table_name not in scoped_tables:
                    scoped_tables.append(table_name)
        scoped_tables = scoped_tables[:TABLE_SEARCH_K]
        scoped_set = set(scoped_tables)

        if self._column_vector_store is not None:
            # 第二段①：表作用域内逐表检索字段，每表按配额收敛
            for table_name in scoped_tables:
                try:
                    docs_with_scores = (
                        self._column_vector_store.similarity_search_with_score(
                            mention,
                            k=COLUMN_SEARCH_K,
                            filter={"table": table_name},
                            fetch_k=max(COLUMN_SEARCH_K * 5, 50),
                        )
                    )
                except Exception:
                    # 单表检索异常不阻断门禁
                    continue
                for doc, distance in docs_with_scores[:PER_TABLE_COLUMN_QUOTA]:
                    raw_candidates.append(self._doc_to_candidate(
                        doc, distance, table_hit=True,
                    ))

            # 第二段②：全局字段检索兜底，避免表级召回漏召导致真实字段丢失
            try:
                docs_with_scores = (
                    self._column_vector_store.similarity_search_with_score(
                        mention,
                        k=COLUMN_SEARCH_K,
                    )
                )
            except Exception:
                docs_with_scores = []
            for doc, distance in docs_with_scores:
                raw_candidates.append(self._doc_to_candidate(
                    doc,
                    distance,
                    table_hit=str(doc.metadata.get("table", "")).strip().lower() in scoped_set,
                ))

        # Planner 全局候选过滤：指标概念按概念词匹配；维度概念要求语义相关
        # （字面匹配，或该字段出现在本概念向量检索的召回集合中），
        # 避免“负责人”概念混入 company_business_type 这类高分但不相关的维度字段
        recalled_fields = {
            (str(item.get("table") or "").strip().lower(), str(item.get("field") or ""))
            for item in raw_candidates
            if item.get("field")
        }
        for candidate in planner_candidates or []:
            if not isinstance(candidate, dict) or not candidate.get("field"):
                continue
            if concept_type == "dimension":
                cand_key = (str(candidate.get("table") or "").strip().lower(), str(candidate.get("field") or ""))
                if not _is_dimension_candidate(candidate) and not _mention_matches_candidate(mention, candidate):
                    continue
                if not _mention_matches_candidate(mention, candidate) and cand_key not in recalled_fields:
                    continue
            elif not _mention_matches_candidate(mention, candidate):
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
                "table_hit": str(candidate.get("table") or "").strip().lower() in scoped_set,
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
                "table_hit": str(candidate.get("table") or "").strip().lower() in scoped_set,
            })

        # 指标概念度量优先，维度概念维度优先；全部被过滤则保留全部（兼容旧元数据缺少 fields_type）
        if concept_type == "dimension":
            dimension_candidates = [
                candidate for candidate in raw_candidates
                if _is_dimension_candidate(candidate)
            ]
            matched = dimension_candidates or raw_candidates
        else:
            measure_candidates = [
                candidate for candidate in raw_candidates
                if _is_measure_candidate(candidate)
            ]
            matched = measure_candidates or raw_candidates

        # 表作用域优先 + 优秀案例加权 + 分数下限 + 每表配额 + 数量上限收敛
        ranked = self._rank_candidates(
            self._deduplicate_candidates(matched),
            target_tables=target_tables,
            example_fields=example_fields,
            truncate=truncate,
        )
        log_metric_event(
            "candidate_recall",
            mention=mention,
            table_scope_count=len(scoped_tables),
            raw_candidates=len(raw_candidates),
            ranked_candidates=len(ranked),
        )
        return ranked

    def _doc_to_candidate(self, doc, distance: float, table_hit: bool) -> dict:
        """把向量检索文档转成候选字典；距离转相似度分数，并标记是否命中表作用域。"""
        metadata = doc.metadata or {}
        page_content = doc.page_content or ""
        return {
            "table": metadata.get("table", ""),
            "field": metadata.get("column", metadata.get("field", "")),
            "semantic_type": metadata.get("fields_type", ""),
            "comment": page_content,
            "aliases": self._extract_aliases(page_content),
            "score": float(round(float(distance), 4)),
            "table_hit": table_hit,
        }

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
            existing["table_hit"] = (
                bool(existing.get("table_hit"))
                or bool(candidate.get("table_hit"))
            )
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
        truncate: bool = True,
    ) -> list[dict]:
        """候选收敛：表作用域/目标表优先、相似度下限过滤、每表配额、数量上限截断。

        收敛不改变“多候选必须追问”语义：过滤后只剩 1 个但原始候选多于 1 个时，
        保留前 2 个继续追问，避免截断把歧义误判为唯一口径。
        truncate=False 时返回精选前全量，供模型受限重排挑选展示候选。
        """
        target_set = {
            str(item).strip().lower()
            for item in (target_tables or [])
            if str(item).strip()
        }

        def _primary(item: dict) -> int:
            """表作用域/目标表命中优先：命中=1，兜底=0。"""
            if item.get("table_hit"):
                return 1
            table_name = str(item.get("table") or "").strip().lower()
            return 1 if target_set and table_name in target_set else 0

        # 相似度下限：低于阈值视为不相关；过滤为空时回退全量，兼容无分数旧元数据。
        filtered = [
            candidate for candidate in candidates
            if float(candidate.get("score") or 0) >= MIN_CANDIDATE_SCORE
        ]
        if not filtered:
            filtered = list(candidates)

        # 优秀案例命中字段排序加权：仅影响展示顺序，不产生解析证据
        example_set = {
            str(field).strip().lower()
            for field in (example_fields or [])
            if str(field).strip()
        }

        def _effective_score(item: dict) -> float:
            base = float(item.get("score") or 0)
            if example_set and str(item.get("field") or "").lower() in example_set:
                return base + EXAMPLE_FIELD_BOOST
            return base

        filtered.sort(
            key=lambda item: (_primary(item), _effective_score(item)),
            reverse=True,
        )

        # 每表配额：表作用域/目标表内字段每表最多保留 PER_TABLE_COLUMN_QUOTA 个，
        # 防止单表字段占满候选名额；全局兜底字段不额外限配额
        quota_counts: dict[str, int] = {}
        ranked: list[dict] = []
        for item in filtered:
            if _primary(item) == 1:
                table_name = str(item.get("table") or "").strip().lower()
                if quota_counts.get(table_name, 0) >= PER_TABLE_COLUMN_QUOTA:
                    continue
                quota_counts[table_name] = quota_counts.get(table_name, 0) + 1
            ranked.append(item)

        if not truncate:
            return ranked

        truncated = ranked[:MAX_AMBIGUITY_CANDIDATES]
        # 多候选不允许被收敛成单候选，避免绕过用户确认
        if len(ranked) >= 2 and len(truncated) < 2:
            truncated = ranked[:2]
        return truncated

    def _keep_previous_resolution(
        self,
        previous: dict,
        candidates: list[dict],
    ) -> bool:
        """已解析记录在用户本轮未改选时继续有效（改选由 LLM 提交的解析字段体现）。"""
        if not previous or previous.get("status") != "resolved":
            return False
        selected_field = previous.get("selected_field", "")
        if not selected_field:
            return False
        # 同时认可上轮真实候选，避免重新召回或候选重排使已确认字段失效。
        valid_fields = {
            candidate.get("field", "") for candidate in candidates
        }
        valid_fields.update(
            candidate.get("field", "")
            for candidate in (previous.get("candidates") or [])
            if isinstance(candidate, dict)
        )
        if selected_field not in valid_fields:
            return False
        return True

    def _resolve_from_llm(
        self,
        mention: str,
        llm_field: str,
        previous: dict,
        candidates: list[dict],
        concept_type: str = "metric",
    ) -> dict:
        """按 LLM 提交的字段构造解析记录；字段非法时返回 None 交由后续判定。"""
        # 兼容 "db.table.field" 完整路径：拆出物理字段名再校验
        if "." in llm_field:
            llm_field = llm_field.rsplit(".", 1)[-1]
        valid_fields = {
            candidate.get("field", "") for candidate in candidates
        }
        previous_field = (previous or {}).get("selected_field", "")
        if previous_field:
            # 上轮已确认字段作为兜底，避免跨轮候选重排导致合法选择被误判
            valid_fields.add(previous_field)
        if llm_field not in valid_fields:
            return None
        if previous and previous_field == llm_field:
            # 与上轮一致时保留上轮解析证据，避免解析链断裂
            return previous
        matched = next(
            (candidate for candidate in candidates
             if candidate.get("field") == llm_field),
            None,
        )
        if matched is None:
            # 仅出现在上轮记录时按上轮结果处理
            return previous
        return self._build_resolution(
            mention, matched, "llm_submitted", concept_type=concept_type,
        )

    # ── 结果构造 ────────────────────────────────────────────

    @staticmethod
    def _build_resolution(
        mention: str,
        candidate: dict,
        source: str,
        concept_type: str = "metric",
    ) -> dict:
        result = {
            "mention": mention,
            "concept_type": concept_type,
            "status": "resolved",
            "selected_field": candidate.get("field", ""),
            "selected_table": candidate.get("table", ""),
            "resolution_source": source,
            "candidates": [candidate],
        }
        # 过滤型候选：保留口径过滤信息，供 to_plan_resolutions 落 filters
        if _is_filter_candidate(candidate):
            result["semantic_type"] = "filter"
            for key in (
                "metric_id", "metric_name",
                "filter_field", "filter_value",
                "filter_label", "filter_model",
            ):
                if candidate.get(key):
                    result[key] = candidate[key]
        return result

    @staticmethod
    def _build_ambiguity(
        mention: str,
        candidates: list[dict],
        concept_type: str = "metric",
    ) -> dict:
        return {
            "mention": mention,
            "concept_type": concept_type,
            "status": "ambiguous",
            "selected_field": "",
            "selected_table": "",
            "resolution_source": "",
            "candidates": candidates,
        }

    @staticmethod
    def _build_meaning(candidate: dict) -> str:
        """从候选提取一句简洁中文含义：原始备注 > 首个别名 > 短说明 > 注释截断。"""
        comment = str(candidate.get("comment") or "")
        # 过滤型候选：口径 = 指标名（过滤标签），按过滤字段=值过滤
        if _is_filter_candidate(candidate):
            metric_name = candidate.get("metric_name", "")
            label = candidate.get("filter_label", "")
            filter_field = candidate.get("filter_field", "")
            filter_value = candidate.get("filter_value", "")
            head = f"{metric_name}（{label}）" if metric_name and label else (label or "")
            return f"{head}，按 {filter_field}={filter_value} 过滤"
        # 原始备注优先，确保展示口径与报表自带备注一致
        for line in comment.splitlines():
            if line.startswith("原始备注:"):
                raw = line[len("原始备注:"):].strip()[:60]
                if raw:
                    return raw
        # 无原始备注时使用首个别名
        aliases = [
            str(alias) for alias in (candidate.get("aliases") or [])
            if str(alias).strip()
        ]
        if aliases:
            return aliases[0][:60]
        # 简短纯说明（非元数据全文）直接使用，如“净增订单数”
        if comment and "字段:" not in comment and len(comment) <= 60:
            return comment
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
            option = {
                "index": index,
                "field": candidate.get("field", ""),
                "table": table,
                "table_short": table_short,
                "meaning": MetricAmbiguityValidator._build_meaning(candidate),
                "comment": str(candidate.get("comment") or "")[:200],
            }
            # 过滤型候选：保留口径过滤信息，供展示/解析识别
            if _is_filter_candidate(candidate):
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
