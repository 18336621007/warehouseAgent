# SemanticLayerProvider: 解析哈尔滨语义层 YAML 目录，提供统一访问接口
#
# 设计要点
# - 单例模式，懒加载；目录变化时支持 reload
# - 物理表 partition 直接来自 YAML，是程序化正确答案，避免再让向量库猜测
# - 指标按 subject（order / asset）目录组织，按 id 索引
# - Join 契约（join_contracts.yaml）按 left_model+right_model 双向索引
# - 实体（entities.yaml）按别名索引，便于"电池""经销商"等业务概念定位
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Optional, Iterable

import yaml


# 语义层根目录：默认指向 Python 包自身所在目录（__file__ 所在目录）
# 外部资源可覆盖（通过构造函数传参），方便开发调试
_DEFAULT_SEMANTIC_LAYER_ROOT = str(Path(__file__).parent)

class SemanticLayerProvider:
    """统一访问 哈尔滨语义层 YAML 目录，提供指标/实体/模型/Join 契约查询接口。"""

    def __init__(self, root_path: str = _DEFAULT_SEMANTIC_LAYER_ROOT):
        self.root_path = root_path
        self._lock = threading.RLock()
        # 缓存
        self._entities: dict[str, dict] = {}            # entity_key -> entity dict
        self._entity_aliases: dict[str, str] = {}       # alias_lower -> entity_key
        self._physical_tables: dict[str, dict] = {}     # full_name -> physical dict
        self._semantic_models: dict[str, dict] = {}     # model_id -> model dict
        self._metrics: dict[str, dict] = {}             # metric_id -> metric dict
        self._metric_aliases: dict[str, str] = {}       # alias_lower -> metric_id
        self._relationships: dict[str, list[dict]] = {} # from_model -> [rel]
        self._join_contracts: dict[str, dict] = {}      # "{left}|{right}" -> contract
        self._join_contracts_by_table: dict[str, list[dict]] = {}  # model_id -> [contract]
        self._loaded = False

    # ── 加载 ─────────────────────────────────────────────────

    def load(self) -> None:
        """加载整个语义层（线程安全，幂等）"""
        with self._lock:
            if self._loaded:
                return
            if not os.path.isdir(self.root_path):
                # 语义层缺失时降级为空，所有查询返回空集合，不阻断主流程
                self._loaded = True
                return

            self._load_entities()
            self._load_physical_tables()
            self._load_semantic_models()
            self._load_metrics()
            self._load_relationships()
            self._load_join_contracts()
            self._loaded = True

    def reload(self) -> None:
        with self._lock:
            self._loaded = False
            self._entities.clear()
            self._entity_aliases.clear()
            self._physical_tables.clear()
            self._semantic_models.clear()
            self._metrics.clear()
            self._metric_aliases.clear()
            self._relationships.clear()
            self._join_contracts.clear()
            self._join_contracts_by_table.clear()
            self.load()

    def _load_yaml(self, path: str) -> Optional[dict]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else None
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def _load_entities(self) -> None:
        data = self._load_yaml(os.path.join(self.root_path, "entities.yaml"))
        for key, info in (data or {}).get("entities", {}).items():
            # id 保留实体语义名（如 dealer），key 可能被 info 中的物理键覆盖（如 company_id）
            self._entities[key] = {"id": key, "key": key, **info}
            for alias in info.get("aliases", []) or []:
                self._entity_aliases[str(alias).strip().lower()] = key

    def _load_physical_tables(self) -> None:
        physical_dir = os.path.join(self.root_path, "physical")
        if not os.path.isdir(physical_dir):
            return
        for schema in os.listdir(physical_dir):
            schema_dir = os.path.join(physical_dir, schema)
            if not os.path.isdir(schema_dir):
                continue
            for fname in os.listdir(schema_dir):
                if not fname.endswith(".yaml"):
                    continue
                data = self._load_yaml(os.path.join(schema_dir, fname))
                if not data or "table" not in data:
                    continue
                table_info = data["table"]
                # 分区字段日期格式：读取字段级 format 声明（如 pt_dt: format: yyyyMMdd）
                field_formats = {
                    field_name: str(field_info["format"])
                    for field_name, field_info in (table_info.get("fields", {}) or {}).items()
                    if isinstance(field_info, dict) and field_info.get("format")
                }
                # full_name = "{schema}.{name}"（与项目内 TableCoverageAnalyzer 用的格式一致）
                full_name = f"{table_info.get('schema', schema)}.{table_info.get('name', fname[:-5])}"
                self._physical_tables[full_name] = {
                    "full_name": full_name,
                    "schema": table_info.get("schema", schema),
                    "name": table_info.get("name", ""),
                    "database": table_info.get("database", "hive"),
                    "hive_table": table_info.get("table", ""),
                    "partition": list(table_info.get("partition", []) or []),
                    "fields": dict(table_info.get("fields", {}) or {}),
                    "field_formats": field_formats,
                    "candidate_keys": list(
                        (table_info.get("grain") or {}).get("candidate_keys", []) or []
                    ),
                    "physical_id": table_info.get("id", full_name),
                }

    @staticmethod
    def _normalize_model_dimensions(dimensions: dict) -> dict:
        """归一化模型维度声明：兼容 values 的两种写法（裸值字符串 / {value, aliases}），
        统一为 [{"value": str, "aliases": [str]}]，供维度枚举值候选发现使用。"""
        normalized = {}
        for key, info in (dimensions or {}).items():
            if not isinstance(info, dict):
                normalized[key] = info
                continue
            entry = dict(info)
            raw_values = entry.get("values") or []
            normalized_values = []
            for item in raw_values:
                if isinstance(item, dict):
                    normalized_values.append({
                        "value": str(item.get("value", "")),
                        "aliases": list(item.get("aliases") or []),
                    })
                else:
                    # 裸值字符串：无别名，value 即本身
                    normalized_values.append({
                        "value": str(item),
                        "aliases": [],
                    })
            if normalized_values:
                entry["values"] = normalized_values
            normalized[key] = entry
        return normalized


    def _load_semantic_models(self) -> None:
        models_dir = os.path.join(self.root_path, "semantic_models")
        if not os.path.isdir(models_dir):
            return
        for schema in os.listdir(models_dir):
            schema_dir = os.path.join(models_dir, schema)
            if not os.path.isdir(schema_dir):
                continue
            for fname in os.listdir(schema_dir):
                if not fname.endswith(".yaml"):
                    continue
                data = self._load_yaml(os.path.join(schema_dir, fname))
                if not data or "model" not in data:
                    continue
                model_info = data["model"]
                model_id = model_info.get("id", f"{schema}.{fname[:-5]}")
                self._semantic_models[model_id] = {
                    "id": model_id,
                    "name": model_info.get("name", ""),
                    "subject": model_info.get("subject", ""),
                    "grain": model_info.get("grain", {}),
                    "source_physical": model_info.get("source", ""),
                    "dimensions": self._normalize_model_dimensions(
                        model_info.get("dimensions", {}) or {}
                    ),
                    "measures": dict(model_info.get("measures", {}) or {}),
                }

    def _load_metrics(self) -> None:
        metrics_dir = os.path.join(self.root_path, "metrics")
        if not os.path.isdir(metrics_dir):
            return
        # 动态发现 metrics/ 下所有主题子目录（后续新增主题无需改代码）
        for subject in sorted(os.listdir(metrics_dir)):
            subject_dir = os.path.join(metrics_dir, subject)
            if not os.path.isdir(subject_dir):
                continue
            for fname in os.listdir(subject_dir):
                if not fname.endswith(".yaml"):
                    continue
                data = self._load_yaml(os.path.join(subject_dir, fname))
                if not data or "metric" not in data:
                    continue
                metric_info = data["metric"]
                metric_id = metric_info.get("id", fname[:-5])
                self._metrics[metric_id] = {
                    "id": metric_id,
                    "name": metric_info.get("name", ""),
                    "aliases": list(metric_info.get("aliases", []) or []),
                    "definition": metric_info.get("definition", ""),
                    "source_model": metric_info.get("source_model", ""),
                    "expression": metric_info.get("expression", ""),
                    "unit": metric_info.get("unit", ""),
                    "dimensions": list(metric_info.get("dimensions", []) or []),
                    # 维度子项型指标族（如电柜状态：激活/在线/离线），供构建器按子口径解析 {field}
                    "dimensional_measures": list(metric_info.get("dimensional_measures", []) or []),
                    "notes": list(metric_info.get("notes", []) or []),
                    "subject": subject,
                    "file_path": os.path.join("metrics", subject, fname),
                    # 明细型指标标记（query_type: detail）随指标一起暴露，供构建器识别
                    "query_type": metric_info.get("query_type", ""),
                }
                # 别名反向索引（lowercase）
                for alias in self._metrics[metric_id]["aliases"]:
                    self._metric_aliases[str(alias).strip().lower()] = metric_id
                # 中文名也参与匹配
                metric_name = self._metrics[metric_id]["name"]
                if metric_name:
                    self._metric_aliases[str(metric_name).strip().lower()] = metric_id

    def _load_relationships(self) -> None:
        rels_dir = os.path.join(self.root_path, "relationships")
        if not os.path.isdir(rels_dir):
            return
        for schema in os.listdir(rels_dir):
            schema_dir = os.path.join(rels_dir, schema)
            if not os.path.isdir(schema_dir):
                continue
            for fname in os.listdir(schema_dir):
                if not fname.endswith(".yaml"):
                    continue
                data = self._load_yaml(os.path.join(schema_dir, fname))
                if not data or "relationships" not in data:
                    continue
                for rel_id, rel in data["relationships"].items():
                    rel_entry = {"id": rel_id, **rel}
                    from_model = rel.get("from_model", "")
                    if from_model:
                        self._relationships.setdefault(from_model, []).append(rel_entry)

    def _load_join_contracts(self) -> None:
        # 按表对拆分：join_contracts/<left_schema>/<left>_to_<right>.yaml，每个文件可含该表对的多个关联语义变体
        contracts_dir = os.path.join(self.root_path, "join_contracts")
        all_joins: dict[str, dict] = {}
        if os.path.isdir(contracts_dir):
            for schema in os.listdir(contracts_dir):
                schema_dir = os.path.join(contracts_dir, schema)
                if not os.path.isdir(schema_dir):
                    continue
                for fname in os.listdir(schema_dir):
                    if not fname.endswith(".yaml"):
                        continue
                    data = self._load_yaml(os.path.join(schema_dir, fname))
                    for cid, contract in (data or {}).get("joins", {}).items():
                        all_joins[cid] = contract
        # 兼容旧单文件 join_contracts.yaml（迁移期间保留）
        legacy = self._load_yaml(os.path.join(self.root_path, "join_contracts.yaml"))
        for cid, contract in (legacy or {}).get("joins", {}).items():
            all_joins.setdefault(cid, contract)
        for cid, contract in all_joins.items():
            left = contract.get("left_model", "")
            right = contract.get("right_model", "")
            if not left or not right:
                continue
            entry = {"id": cid, **contract}
            # 双向索引（同一表对存在多个关联语义变体时，按 id 追加；对表级查询取全部变体）
            self._join_contracts[f"{left}|{right}"] = entry
            self._join_contracts[f"{right}|{left}"] = entry
            self._join_contracts_by_table.setdefault(left, []).append(entry)
            self._join_contracts_by_table.setdefault(right, []).append(entry)

    # ── 实体查询 ─────────────────────────────────────────────

    def get_entity_by_key(self, entity_key: str) -> Optional[dict]:
        self.load()
        return self._entities.get(entity_key)

    def get_entity_by_keyword(self, keyword: str) -> Optional[dict]:
        self.load()
        key = self._entity_aliases.get(str(keyword or "").strip().lower())
        if key:
            return self._entities.get(key)
        # 模糊匹配：别名包含 keyword
        keyword_lower = str(keyword or "").strip().lower()
        for alias, ent_key in self._entity_aliases.items():
            if keyword_lower and keyword_lower in alias:
                return self._entities.get(ent_key)
        return None

    def get_all_entities(self) -> list[dict]:
        self.load()
        return list(self._entities.values())

    # ── 物理表 / 模型查询 ─────────────────────────────────────

    def get_physical_table(self, full_name: str) -> Optional[dict]:
        self.load()
        return self._physical_tables.get(full_name)

    def get_physical_table_by_hive_name(self, hive_table: str) -> Optional[dict]:
        """通过完整 hive 表名（hive.schema.table）查找物理表"""
        self.load()
        for info in self._physical_tables.values():
            if info.get("hive_table") == hive_table:
                return info
        return None

    def get_partition_fields(self, full_name: str) -> list[str]:
        """获取物理表的分区字段列表（pt_dt、pt_platform 等），是程序的唯一真实来源"""
        info = self.get_physical_table(full_name)
        return list(info.get("partition", []) or []) if info else []

    def get_partition_field_format(self, full_name: str, field: str = "pt_dt") -> str:
        """获取物理表分区字段的日期格式（如 yyyyMMdd），语义层未声明时默认 yyyyMMdd"""
        normalized = full_name[len("physical."):] if full_name.startswith("physical.") else full_name
        info = self.get_physical_table(normalized)
        formats = (info or {}).get("field_formats") or {}
        return formats.get(field) or "yyyyMMdd"

    def get_table_fields(self, full_name: str) -> list[str]:
        """获取物理表所有字段名列表"""
        info = self.get_physical_table(full_name)
        return list(info.get("fields", {}).keys()) if info else []

    def is_field_in_table(self, full_name: str, field_name: str) -> bool:
        info = self.get_physical_table(full_name)
        return bool(info and field_name in info.get("fields", {}))

    def get_all_physical_tables(self) -> list[dict]:
        self.load()
        return list(self._physical_tables.values())

    def get_semantic_model(self, model_id: str) -> Optional[dict]:
        self.load()
        return self._semantic_models.get(model_id)

    def get_all_semantic_models(self) -> list[dict]:
        self.load()
        return list(self._semantic_models.values())

    # ── 指标查询 ─────────────────────────────────────────────

    def get_metric_by_id(self, metric_id: str) -> Optional[dict]:
        self.load()
        return self._metrics.get(metric_id)

    def search_metrics(self, query: str, limit: int = 5) -> list[dict]:
        """按名称/别名/定义关键词搜索指标"""
        self.load()
        if not query:
            return []
        keyword = str(query).strip().lower()
        results = []
        for metric in self._metrics.values():
            score = 0
            aliases_lower = [str(a).strip().lower() for a in metric["aliases"]]
            name_lower = metric["name"].lower()
            if keyword in name_lower:
                score += 5
            for alias in aliases_lower:
                if keyword == alias:
                    score += 10
                elif keyword in alias:
                    score += 3
            definition_lower = metric["definition"].lower()
            if keyword in definition_lower:
                score += 1
            if score > 0:
                results.append((score, metric))
        results.sort(key=lambda item: (-item[0], item[1]["id"]))
        return [m for _, m in results[:limit]]

    def match_metrics_from_query(self, query: str) -> list[dict]:
        """从用户问题匹配指标：分词后逐个命中，按命中强度排序"""
        self.load()
        if not query:
            return []
        # 简单分词：使用中英文常见分隔符
        import re
        tokens = re.split(r"[\s,，、。;；:：]+", str(query))
        tokens = [t.strip() for t in tokens if t and t.strip()]
        # 同时保留整句，用于精确子串匹配；与分词结果重复时不再追加，避免分数翻倍
        whole = str(query).strip()
        if whole and whole not in tokens:
            tokens.append(whole)

        scores: dict[str, int] = {}
        for token in tokens:
            token_lower = token.lower()
            if not token_lower:
                continue
            for alias_lower, metric_id in self._metric_aliases.items():
                if token_lower == alias_lower:
                    scores[metric_id] = scores.get(metric_id, 0) + 10
                elif token_lower in alias_lower or alias_lower in token_lower:
                    # 前缀/近义（如“新增订单”对“新增订单数”）视为高度对应，加权更高
                    if alias_lower.startswith(token_lower) or token_lower.startswith(alias_lower):
                        scores[metric_id] = scores.get(metric_id, 0) + 6
                    else:
                        scores[metric_id] = scores.get(metric_id, 0) + 3
        matched = []
        for metric_id, score in scores.items():
            metric = self._metrics[metric_id]
            matched.append({"score": score, "metric": metric})
        matched.sort(key=lambda item: (-item["score"], item["metric"]["id"]))
        # 返回带 score/confidence 的指标字典，供上层按置信度判断"语义层唯一命中"
        # （Planner _semantic_unique）：完全相等=1.0，前缀/近义=0.9，一般子串=0.7
        return [
            {
                **m["metric"],
                "score": m["score"],
                "confidence": self._to_confidence(m["score"]),
            }
            for m in matched
        ]

    def grep_metrics(self, keywords: list[str], limit: int = 5) -> list[dict]:
        """全文 grep 检索指标（对齐 skill 关键词 grep 思路）：对 id/name/aliases/
        definition/notes/dimensions 全文匹配，区分强命中（名称/别名）与弱命中
        （定义/备注/维度），按权重排序截断 top-k。

        返回带 hit_type/strong_hits/weak_hits/grep_score/confidence 的指标字典；
        命中即算候选，最终置信度由 Planner LLM 判定（第3层）。
        """
        self.load()
        keywords = [
            str(k).strip().lower()
            for k in (keywords or [])
            if str(k).strip()
        ]
        if not keywords:
            return []
        results = []
        for metric in self._metrics.values():
            name_l = str(metric.get("name", "")).lower()
            aliases_l = [
                str(a).strip().lower()
                for a in (metric.get("aliases") or [])
                if str(a).strip()
            ]
            definition_l = str(metric.get("definition", "")).lower()
            notes_l = " ".join(
                str(n) for n in (metric.get("notes") or [])
            ).lower()
            dims_l = " ".join(
                str(d) for d in (metric.get("dimensions") or [])
            ).lower()
            strong_score = 0
            weak_score = 0
            strong_hits = []
            weak_hits = []
            for kw in keywords:
                if kw in name_l or any(kw in a for a in aliases_l):
                    # 名称/别名命中：强命中，权重最高
                    strong_score += 6
                    strong_hits.append(kw)
                elif kw in definition_l:
                    weak_score += 3
                    weak_hits.append(kw)
                elif kw in notes_l:
                    weak_score += 2
                    weak_hits.append(kw)
                elif kw in dims_l:
                    weak_score += 1
                    weak_hits.append(kw)
            if not strong_score and not weak_score:
                continue
            score = strong_score + weak_score
            hit_type = "strong" if strong_score > 0 else "weak"
            results.append({
                **metric,
                "grep_score": score,
                "hit_type": hit_type,
                "strong_hits": strong_hits,
                "weak_hits": weak_hits,
                "confidence": self._to_grep_confidence(score, hit_type),
            })
        results.sort(key=lambda item: (-item["grep_score"], item["id"]))
        return results[:limit]

    @staticmethod
    def _to_grep_confidence(score: int, hit_type: str) -> float:
        """grep 词法命中 → 置信度提示（供 Planner LLM 参考，最终以 LLM 判定为准）：
        名称/别名强命中=0.9，定义/备注弱命中=0.7。"""
        if hit_type == "strong":
            return 0.9
        return 0.7

    def grep_metric_files(self, keywords: list[str], limit: int = 3) -> list[dict]:
        """文件系统 grep 定位指标文件（对齐 skill 的文件 grep 思路）。

        - 遍历 load() 时按文件系统扫描建立的指标索引（metrics/<subject>/*.yaml），
          只匹配 id/name/aliases（不全文匹配 notes/definition，减少弱相关噪声）；
        - 双向子串匹配：关键词是名称/别名子串，或名称/别名是关键词子串（兼容整句/多字变体）；
        - 排序：名称/别名精确命中优先，再按包含方向，同级按 id；
        - 返回完整 metric dict（notes/枚举全量）并附加 file_path 字段，供上层注入完整口径。
        """
        self.load()
        keywords = [str(k).strip().lower() for k in (keywords or []) if str(k).strip()]
        if not keywords:
            return []
        results = []
        for metric in self._metrics.values():
            score, hit_kws, exact = self._score_metric_keywords(metric, keywords)
            if score <= 0:
                continue
            _m = dict(metric)
            _m["hit_type"] = "strong"
            _m["strong_hits"] = hit_kws
            _m["weak_hits"] = []
            _m["grep_score"] = score
            _m["confidence"] = 1.0 if exact else 0.9
            results.append(_m)
        results.sort(key=lambda item: (-item["grep_score"], item["id"]))
        return results[:limit]

    @staticmethod
    def _score_metric_keywords(metric: dict, keywords: list[str]) -> tuple:
        """对单个指标按 id/name/aliases 打分（文件系统 grep 的匹配与排序依据）。

        - 精确（name/alias == 关键词）权重最高；关键词是名称/别名子串次之；名称/别名是关键词子串再次；
        - 返回 (score, hit_kws, exact)：exact 表示存在精确命中，供置信度提示用。
        """
        name_l = str(metric.get("name") or "").lower()
        aliases_l = [str(a).lower() for a in (metric.get("aliases") or []) if str(a).strip()]
        id_l = str(metric.get("id") or "").lower()
        score = 0
        hit_kws = []
        exact = False
        for kw in keywords:
            if name_l == kw or any(a == kw for a in aliases_l):
                score += 100
                exact = True
                hit_kws.append(kw)
            elif kw in name_l or any(kw in a for a in aliases_l):
                score += 50
                hit_kws.append(kw)
            elif id_l == kw:
                score += 60
                exact = True
                hit_kws.append(kw)
            elif kw in id_l:
                score += 30
                hit_kws.append(kw)
            elif name_l in kw or any(a in kw for a in aliases_l):
                score += 10
                hit_kws.append(kw)
        return score, hit_kws, exact

    def resolve_metric_chain(self, metric_id: str) -> dict:
        """指针导航：按 metric.source_model → semantic_model → physical 逐层打开，
        只返回命中指标的小组信息（对齐 skill "打开小组文件" 思路，不触发向量库）。"""
        self.load()
        metric = self._metrics.get(metric_id)
        if not metric:
            return {}
        source_model = str(metric.get("source_model") or "")
        semantic_model = self._semantic_models.get(source_model)
        physical = None
        if semantic_model:
            physical_key = str(semantic_model.get("source_physical") or "")
            # 兼容 physical. 前缀（physical.ads_trip.xxx）
            if physical_key.startswith("physical."):
                physical_key = physical_key[len("physical."):]
            physical = self._physical_tables.get(physical_key)
        return {
            "metric": metric,
            "semantic_model": semantic_model,
            "physical": physical,
        }


    def discover_dimension_filter_candidates(
        self,
        mention_terms: list[str],
        matched_metrics: list[dict],
        limit: int = 10,
    ) -> list[dict]:
        """维度枚举值候选发现：对每个命中指标，遍历其 source_model + join 契约可达模型的
        维度 values，用 mention 词（指标/维度说法）匹配枚举值，生成"指标+过滤"型候选。

        候选 semantic_type="filter"，带 filter_field/filter_value/filter_label；
        范围收敛到指标主表 + join 契约可达模型，避免全量模型候选噪声。
        只做候选发现，不产生解析证据，最终口径由用户确认。
        """
        self.load()
        if not mention_terms or not matched_metrics:
            return []
        terms = [
            str(t).strip().lower()
            for t in mention_terms
            if str(t).strip()
        ]
        if not terms:
            return []
        candidates: list[dict] = []
        seen: set[tuple] = set()
        for metric in matched_metrics:
            source_model = str(metric.get("source_model") or "")
            if not source_model:
                continue
            # 可达模型：指标主表 + join 契约两侧，避免全量模型候选噪声
            scope_ids = {source_model}
            for contract in self.get_join_contracts_for_model(source_model):
                scope_ids.add(str(contract.get("left_model") or ""))
                scope_ids.add(str(contract.get("right_model") or ""))
            measure_field = self._extract_measure_field(
                metric.get("expression", ""), metric.get("id", "")
            )
            for model_id in scope_ids:
                model = self._semantic_models.get(model_id)
                if not model:
                    continue
                for dim_key, dim_info in (model.get("dimensions") or {}).items():
                    if not isinstance(dim_info, dict):
                        continue
                    values = dim_info.get("values") or []
                    if not values:
                        continue
                    field = str(dim_info.get("field") or "")
                    if not field:
                        continue
                    for item in values:
                        value = str(item.get("value") or "")
                        if not value:
                            continue
                        aliases = [str(a) for a in (item.get("aliases") or [])]
                        if not self._match_value_to_terms(value, aliases, terms):
                            continue
                        key = (metric.get("id", ""), field, value)
                        if key in seen:
                            continue
                        seen.add(key)
                        label = aliases[0] if aliases else value
                        # 合成 field：保证过滤候选与同表度量字段候选在展示/去重时不冲突
                        synthetic_field = (
                            f"__filter__{metric.get('id', '')}"
                            f"__{field}__{value}"
                        )
                        candidates.append({
                            "table": source_model,
                            "field": synthetic_field,
                            "semantic_type": "filter",
                            "metric_id": metric.get("id", ""),
                            "metric_name": metric.get("name", ""),
                            "filter_field": field,
                            "filter_value": value,
                            "filter_label": label,
                            "filter_model": model_id,
                            "comment": (
                                f"{metric.get('name', '')} 按 {dim_key}={value}"
                                f"（{label}）过滤，过滤字段 {field} 位于 {model_id}"
                            ),
                            "score": 1.0,
                        })
        return candidates[:limit]

    @staticmethod
    def _match_value_to_terms(value: str, aliases: list[str], terms: list[str]) -> bool:
        """枚举值与用户说法匹配：相等 / 别名相等 / 说法去后缀后等于裸值。"""
        value_l = str(value or "").strip().lower()
        alias_l = [str(a).strip().lower() for a in aliases if str(a).strip()]
        for t in terms:
            if not t:
                continue
            if t == value_l:
                return True
            if any(t == a for a in alias_l):
                return True
            # 去"类/级/型/类型"后缀后与裸值相等（兼容未补别名的英文代码维度值）
            stripped = t
            for suffix in ("类型", "类", "级", "型"):
                if stripped.endswith(suffix):
                    stripped = stripped[:-len(suffix)]
                    break
            if stripped and stripped == value_l:
                return True
        return False

    @staticmethod
    def _extract_measure_field(expression: str, metric_id: str) -> str:
        """从聚合表达式提取主度量字段，缺省回退指标 id。"""
        if not expression:
            return metric_id
        match = re.search(r"\(([^)]*)\)", str(expression))
        if match:
            inner = match.group(1).strip()
            if inner and inner != "{field}":
                return inner
        return metric_id


    @staticmethod
    def _to_confidence(score: int) -> float:
        """整数词法分 → 置信度（0~1）：完全相等=1.0，前缀/近义=0.9，一般子串=0.7。"""
        if score >= 10:
            return 1.0
        if score >= 6:
            return 0.9
        if score >= 3:
            return 0.7
        return 0.4


    def get_all_metrics(self) -> list[dict]:
        self.load()
        return list(self._metrics.values())

    # ── 关系 & Join 契约 ──────────────────────────────────────

    def get_relationships_for_model(self, model_id: str) -> list[dict]:
        self.load()
        return list(self._relationships.get(model_id, []))

    def get_join_contract(
        self, left_model: str, right_model: str
    ) -> Optional[dict]:
        self.load()
        return self._join_contracts.get(f"{left_model}|{right_model}")

    def get_join_contracts_for_model(self, model_id: str) -> list[dict]:
        self.load()
        return list(self._join_contracts_by_table.get(model_id, []))

    def get_all_join_contracts(self) -> list[dict]:
        self.load()
        # 去重（双向索引导致同一合约出现两次）
        seen: set[str] = set()
        result = []
        for key, entry in self._join_contracts.items():
            cid = entry.get("id", key)
            if cid in seen:
                continue
            seen.add(cid)
            result.append(entry)
        return result

    def find_safe_join_path(
        self,
        models: Iterable[str],
    ) -> list[dict]:
        """在给定模型集合中寻找合规的 Join 边列表（BFS）。"""
        self.load()
        models = list(models)
        if len(models) <= 1:
            return []
        adj: dict[str, list[dict]] = {}
        for contract in self._join_contracts_by_table.values():
            pass  # noqa  (iteration only)
        for entry in self._join_contracts.values():
            left = entry.get("left_model", "")
            right = entry.get("right_model", "")
            if left in models and right in models:
                adj.setdefault(left, []).append({"other": right, "edge": entry})
                adj.setdefault(right, []).append({"other": left, "edge": entry})

        from collections import deque
        visited: set[str] = {models[0]}
        queue: deque = deque([models[0]])
        edges: list[dict] = []
        while queue:
            current = queue.popleft()
            for item in adj.get(current, []):
                if item["other"] not in visited:
                    visited.add(item["other"])
                    edges.append(item["edge"])
                    queue.append(item["other"])
        # 还原方向：保证 left/right 与合约一致
        return edges


# 全局单例
_provider_singleton: Optional[SemanticLayerProvider] = None
_provider_lock = threading.Lock()


def get_semantic_layer_provider(
    root_path: str = _DEFAULT_SEMANTIC_LAYER_ROOT,
) -> SemanticLayerProvider:
    """获取全局 SemanticLayerProvider 单例（懒加载）"""
    global _provider_singleton
    if _provider_singleton is None:
        with _provider_lock:
            if _provider_singleton is None:
                _provider_singleton = SemanticLayerProvider(root_path)
    elif _provider_singleton.root_path != root_path:
        # 路径变化时重建
        _provider_singleton = SemanticLayerProvider(root_path)
    _provider_singleton.load()
    return _provider_singleton
