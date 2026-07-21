"""
metadata_enricher.py — 基于 SQL-MARS 论文第四章/第五章的分层元数据自动构建脚本

流程（自底向上反哺）：
  1. 字段级增强：Hive Schema + 采样值 → LLM → field_aliases / fields_type / relations
  2. 表级增强：原始 Schema + 字段增强结果 → LLM → core_function / key_entities / potential_use_cases
  3. 库级增强：表增强结果 → LLM → domain / description / full_table_list

存储：MySQL（enriched_databases / enriched_tables / enriched_columns 三张表）
增量策略：
  - 字段级：逐字段比对，已有跳过，新增采样+LLM
  - 表级：表已有且无新字段 → 跳过；有新字段或新表 → 重跑 LLM
  - 库级：每次全量重跑（开销极小，只有 3 个库）
"""
import json
import os
import re

from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
from agentTest.datasource.hive_datasource import HiveDataSource
from agentTest.llm import LLM
from agentTest.db.hive_guardrails import ALLOWED_DATABASES
from agentTest.metadata.mysql_store import (
    init_metadata_tables,
    column_exists, table_exists,
    save_column, save_table, save_database,
)


# ── 辅助函数：从 LLM 响应中安全提取 JSON ──────────────────────────────
def _parse_json_response(response: str) -> dict:
    """从 LLM 返回的字符串中提取 JSON 对象"""
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {}


# ── 辅助函数：字段采样 ──────────────────────────────────────────────
def _sample_column_values(datasource, database_name, table_name, column_name, limit=3):
    """对单个字段采样 N 条去重非空真实值"""
    sql = (
        f"SELECT DISTINCT {column_name} "
        f"FROM {database_name}.{table_name} "
        f"WHERE {column_name} IS NOT NULL AND {column_name} != '' "
        f"LIMIT {limit}"
    )
    try:
        result = datasource.query(sql)
        return [str(row[0]) for row in result["rows"]]
    except Exception:
        return []  # 采样失败不阻塞流程


# ── Step 1: 字段级增强（含采样） ─────────────────────────────────────
def _enrich_columns(meta_provider, datasource, llm):
    """遍历白名单表的所有字段，采样真实值后调用 LLM 生成字段级增强元数据
    返回: (enriched_columns_dict, new_column_tables_set)
    new_column_tables 用于标记哪些表有新增字段，供 _enrich_tables 判断是否需要重跑表级增强"""
    tables = meta_provider.list_tables()
    result = {}
    new_column_tables = set()  # 记录哪些表有新增字段

    for table_info in tables:
        table_name = table_info["table_name"]
        database_name = table_info["database_name"]
        full_name = f"{database_name}.{table_name}"
        schema = meta_provider.describe_table(table_name)
        total_cols = len(schema["columns"])

        for idx, col in enumerate(schema["columns"]):
            col_name = col["name"]
            col_type = col["type"]
            col_comment = col.get("comment", "") or ""
            key = f"{full_name}.{col_name}"

            # 断点续跑：已增强过的字段跳过
            if column_exists(key):
                print(f"  [{idx+1}/{total_cols}] {key} 已存在，跳过")
                continue

            # 进度提示
            print(f"  [{idx+1}/{total_cols}] {full_name}.{col_name} 采样中...", end=" ")

            # 采样 3 条真实值
            samples = _sample_column_values(
                datasource, database_name, table_name, col_name
            )

            prompt = f"""你是一个数据仓库元数据专家。请根据以下字段信息，按 JSON 格式输出增强元数据。

表名：{full_name}
字段名：{col_name}
字段类型：{col_type}
原始注释：{col_comment or "无"}
采样值（来自真实数据）：{samples if samples else "无采样数据"}

请输出 JSON，只包含以下四个字段（不要输出其他内容）：
{{
  "domain": "该字段所属业务领域（与表保持一致）",
  "fields_type": "dimension 或 measure（状态/平台/渠道/日期等分组字段填 dimension，金额/数量/天数等可聚合数值字段填 measure）",
  "relations": [],
  "field_aliases": ["同义词1", "同义词2", "同义词3"]
}}

注意：
- field_aliases 给出 3-5 个中文同义词或业务别名
- relations 暂时固定为空数组"""

            response = llm.chat([{"role": "user", "content": prompt}])
            parsed = _parse_json_response(response)

            result[key] = {
                "domain": parsed.get("domain", ""),
                "fields_type": parsed.get("fields_type", "dimension"),
                "relations": parsed.get("relations", []),
                "field_aliases": parsed.get("field_aliases", []),
                "_original_comment": col_comment,
            }

            # 标记该表有新增字段，后续表级增强需要重跑
            new_column_tables.add(full_name)

            # 写入 MySQL
            save_column(key, database_name, table_name, col_name, result[key], samples)

            print("✓")

    return result, new_column_tables


# ── Step 2: 表级增强（字段反哺表） ─────────────────────────────────────
def _enrich_tables(meta_provider, llm, enriched_columns, new_column_tables):
    """自底向上增强：用字段级增强结果（维度/度量标记+别名）反哺表级元数据
    仅当表不存在或有新字段时才重跑 LLM"""
    tables = meta_provider.list_tables()
    result = {}
    total_tables = len(tables)

    for idx, table_info in enumerate(tables):
        table_name = table_info["table_name"]
        database_name = table_info["database_name"]
        full_name = f"{database_name}.{table_name}"

        # 断点续跑：表已在 MySQL 中且没有新增字段 → 跳过
        if table_exists(full_name) and full_name not in new_column_tables:
            print(f"  [{idx+1}/{total_tables}] {full_name} 已存在且无新字段，跳过")
            continue

        # 进度提示
        print(f"  [{idx+1}/{total_tables}] {full_name} 增强中...", end=" ")

        schema = meta_provider.describe_table(table_name)

        # 构建包含字段增强信息的上下文（维度/度量标记 + 别名）
        column_details = []
        for col in schema["columns"]:
            col_name = col["name"]
            col_key = f"{full_name}.{col_name}"
            enhanced = enriched_columns.get(col_key, {})
            aliases = "、".join(enhanced.get("field_aliases", []))
            fields_type = enhanced.get("fields_type", "")
            type_tag = "【度量】" if fields_type == "measure" else "【维度】"

            column_details.append(
                f"- {col['name']} ({col['type']}) {type_tag}"
                f"{' 别名: ' + aliases if aliases else ''}"
                f"{' 注释: ' + col.get('comment', '') if col.get('comment') else ''}"
            )

        column_summary = "\n".join(column_details)

        prompt = f"""你是一个数据仓库元数据专家。请根据以下 Hive 表及字段增强信息，按 JSON 格式输出表级增强元数据。

表名：{full_name}
原始注释：{table_info.get("table_comment", "") or "无"}

字段增强信息（含维度/度量标记和业务别名）：
{column_summary}

请输出 JSON，只包含以下四个字段：
{{
  "domain": "该表所属业务领域的中文标签",
  "core_function": "综合字段的维度/度量标记和别名，用一段话描述该表的核心功能和存储内容",
  "key_entities": ["核心实体字段名（如主键）"],
  "potential_use_cases": ["依据标记为 measure 的字段推断的适用分析场景"]
}}

注意：
- core_function 要体现增强信息，如"存储订单明细，包含【度量】金额/天数等可聚合指标和【维度】状态/平台等分组维度"
- potential_use_cases 应依据 measure 字段推断（如金额→补贴分析，天数→租期分析）"""

        response = llm.chat([{"role": "user", "content": prompt}])
        parsed = _parse_json_response(response)

        result[full_name] = {
            "domain": parsed.get("domain", ""),
            "core_function": parsed.get("core_function", ""),
            "key_entities": parsed.get("key_entities", []),
            "potential_use_cases": parsed.get("potential_use_cases", []),
            "_original_comment": table_info.get("table_comment", "") or "",
        }

        # 写入 MySQL
        save_table(full_name, result[full_name])

        print("✓")

    return result


# ── Step 3: 库级增强（表反哺库） ─────────────────────────────────────
def _enrich_databases(meta_provider, llm, enriched_tables):
    """自底向上增强：用表级增强结果反哺库级元数据"""
    result = {}
    total_dbs = len(ALLOWED_DATABASES)

    for idx, db_name in enumerate(ALLOWED_DATABASES):
        # 筛选属于该库的表
        db_tables = {
            k: v for k, v in enriched_tables.items()
            if k.startswith(f"{db_name}.")
        }
        if not db_tables:
            continue

        # 进度提示
        print(f"  [{idx+1}/{total_dbs}] {db_name} 库级增强中...", end=" ")

        # 汇总表 core_function 作为 prompt 上下文
        table_summary = "\n".join([
            f"- {k}: {v.get('core_function', '')}"
            for k, v in db_tables.items()
        ])

        prompt = f"""你是一个数据仓库元数据专家。请根据以下信息，按 JSON 格式输出库级增强元数据。

数据库：{db_name}
该库下表的增强信息：
{table_summary}

请输出 JSON，只包含以下三个字段：
{{
  "domain": "该库所属业务领域的中文标签",
  "full_table_list": {json.dumps(list(db_tables.keys()), ensure_ascii=False)},
  "description": "综合该库下所有表的 core_function，用一段话概括该库的数据定位和核心价值"
}}"""

        response = llm.chat([{"role": "user", "content": prompt}])
        parsed = _parse_json_response(response)

        result[db_name] = {
            "domain": parsed.get("domain", ""),
            "full_table_list": parsed.get("full_table_list", list(db_tables.keys())),
            "description": parsed.get("description", ""),
        }

        # 写入 MySQL
        save_database(db_name, result[db_name])

        print("✓")

    return result


# ── 主入口 ──────────────────────────────────────────────────────────
def build_enriched_metadata(output_path="metadata/enriched_metadata.json"):
    """主流程：字段→表→库 自底向上串联，写入 MySQL + 本地 JSON 备份"""
    # 初始化 MySQL 表结构
    init_metadata_tables()

    meta_provider = HiveMetadataProvider()
    datasource = HiveDataSource()
    llm = LLM()

    # Step 1: 字段级增强（含采样）
    print("Step 1/3: 增强字段级元数据（含采样）...")
    enriched_columns, new_column_tables = _enrich_columns(meta_provider, datasource, llm)
    print(f"  -> 完成 {len(enriched_columns)} 个新字段\n")

    # Step 2: 字段 → 表 反哺
    print("Step 2/3: 字段 → 表 反哺增强...")
    enriched_tables = _enrich_tables(meta_provider, llm, enriched_columns, new_column_tables)
    print(f"  -> 完成 {len(enriched_tables)} 张表\n")

    # Step 3: 表 → 库 反哺
    print("Step 3/3: 表 → 库 反哺增强...")
    enriched_databases = _enrich_databases(meta_provider, llm, enriched_tables)
    print(f"  -> 完成 {len(enriched_databases)} 个库\n")

    output = {
        "databases": enriched_databases,
        "tables": enriched_tables,
        "columns": enriched_columns,
    }

    # 备份到本地 JSON
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"已保存 JSON 备份到 {output_path}")
    return output


if __name__ == "__main__":
    build_enriched_metadata()
