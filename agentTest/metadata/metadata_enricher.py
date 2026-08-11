"""
metadata_enricher.py — 基于 SQL-MARS 论文第四章/第五章的分层元数据自动构建脚本

流程（自底向上反哺）：
  1. 字段级增强：Hive Schema + 采样值 → LLM → field_aliases / fields_type / relations
  2. 表级增强：原始 Schema + 字段增强结果 → LLM → core_function / key_entities / potential_use_cases
  3. 库级增强：表增强结果 → LLM → domain / description / full_table_list

存储：MySQL（enriched_databases / enriched_tables / enriched_columns 三张表）
增量策略：
  - 字段级：逐字段比对，已有跳过，新增采样+LLM
  - 表级：表已有且字段、表注释均未变化 → 跳过；有新字段、新表或表注释变化 → 重跑 LLM
  - 库级：每次全量重跑（开销极小，只有 3 个库）
"""
import json
import os

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from agentTest.config.settings import get_model_name
from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
from agentTest.datasource.hive_datasource import HiveDataSource
from agentTest.db.metadata_scope import get_allowed_databases
from agentTest.metadata.schema_fingerprint import table_fingerprint, diff_columns
from agentTest.langgraph_app.prompts.metadata_enricher_prompt import (
    ColumnEnrichmentOutput,
    TableEnrichmentOutput,
    DatabaseEnrichmentOutput,
    COLUMN_ENRICH_SYSTEM_PROMPT,
    TABLE_ENRICH_SYSTEM_PROMPT,
    DATABASE_ENRICH_SYSTEM_PROMPT,
)
from agentTest.metadata.mysql_store import (
    init_metadata_tables,
    column_exists, table_exists, load_enriched_tables,
    save_column, save_table, save_database,
    get_sync_state, save_sync_state, delete_sync_state, get_all_sync_states,
    delete_table_data, delete_enriched_column, log_metadata_changes,
    EVENT_COLUMN_ADDED, EVENT_COLUMN_MODIFIED, EVENT_COLUMN_REMOVED, EVENT_TABLE_RETIRED,
)


# ── 辅助函数：结构化 LLM（API 层约束输出格式，替代 prompt 声明 JSON） ──
def _build_structured_llm(output_model):
    """构造强制按 Pydantic schema 输出的结构化 LLM，不依赖 prompt 保证 JSON 完整"""
    chat_openai = ChatOpenAI(
        model=get_model_name(),
        temperature=0,  # 元数据增强任务不需要随机性
    )
    return chat_openai.with_structured_output(output_model)


# ── 辅助函数：结构化 LLM 调用（含批处理容错） ────────────────────────
def _invoke_structured(structured_llm, user_content, system_prompt, item_name):
    """调用结构化 LLM：system 定义规则，user 只放数据；失败时回退默认值不中断批处理"""
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]
    try:
        output = structured_llm.invoke(messages)
        return output.model_dump()
    except Exception as exc:
        print(f"  [{item_name}] 结构化输出失败: {exc}，使用默认值")
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
def _enrich_columns(meta_provider, datasource, structured_llm):
    """基于 schema 指纹的字段级增量增强
    返回: (enriched_columns_dict, changed_tables_set)
    - 指纹一致 → 整表跳过（不采样不增强）
    - 指纹变化 → 字段级 diff：新增/修改字段采样 + LLM 增强；删除字段清理 MySQL
    - changed_tables 标记有新增/修改/删除字段的表，供表级增强重跑"""
    meta_provider.clear_tables_cache()
    tables = meta_provider.list_tables()
    result = {}
    changed_tables = set()  # 记录哪些表有字段变化（新增/修改/删除）

    for table_info in tables:
        table_name = table_info["table_name"]
        database_name = table_info["database_name"]
        full_name = f"{database_name}.{table_name}"
        schema = meta_provider.describe_table(table_name)
        columns = schema.get("columns", [])
        total_cols = len(columns)

        # 计算当前指纹（字段名/类型/注释 + 表注释）
        table_fp, col_fps = table_fingerprint(columns, schema.get("table_comment", "") or "")

        # 指纹一致：Hive schema 未变化，整表跳过（增量核心）
        prev = get_sync_state(database_name, table_name)
        if prev and prev.get("table_fingerprint") == table_fp:
            print(f"  [{full_name}] schema 指纹一致，整表跳过")
            continue

        # 字段级 diff：新增 / 修改 / 删除
        if prev:
            prev_fps = prev.get("column_fingerprints") or {}
            added, modified, removed = diff_columns(prev_fps, col_fps)
        else:
            added, modified, removed = list(col_fps.keys()), [], []

        if added or modified or removed:
            changed_tables.add(full_name)

        # 删除字段：清理 MySQL 增强数据 + 审计事件
        for col_name in removed:
            full_key = f"{full_name}.{col_name}"
            delete_enriched_column(full_key)
            log_metadata_changes([{
                "event_type": EVENT_COLUMN_REMOVED,
                "database_name": database_name,
                "table_name": table_name,
                "column_name": col_name,
                "detail": {"full_key": full_key},
            }])
            print(f"  [{full_name}] 字段已删除，清理增强数据: {col_name}")

        need_process = set(added) | set(modified)
        for idx, col in enumerate(columns):
            col_name = col["name"]
            col_type = col["type"]
            col_comment = col.get("comment", "") or ""
            key = f"{full_name}.{col_name}"

            if col_name not in need_process:
                continue

            # 断点续跑：新增字段上次中断前已增强过 → 跳过；修改字段必须强制重增强
            if col_name in added and column_exists(key):
                print(f"  [{idx+1}/{total_cols}] {key} 已存在，跳过")
                continue

            # 进度提示
            print(f"  [{idx+1}/{total_cols}] {full_name}.{col_name} 采样中...", end=" ")

            # 采样 3 条真实值
            samples = _sample_column_values(
                datasource, database_name, table_name, col_name
            )

            prompt = f"""表名：{full_name}
字段名：{col_name}
字段类型：{col_type}
原始注释：{col_comment or "无"}
采样值（来自真实数据）：{samples if samples else "无采样数据"}"""

            parsed = _invoke_structured(
                structured_llm, prompt, COLUMN_ENRICH_SYSTEM_PROMPT,
                f"{full_name}.{col_name}"
            )

            result[key] = {
                "domain": parsed.get("domain", ""),
                "fields_type": parsed.get("fields_type", "dimension"),
                "relations": parsed.get("relations", []),
                "field_aliases": parsed.get("field_aliases", []),
                "_original_comment": col_comment,
                # 备注来源标记：有 DDL 原始备注为 ddl_comment，否则为 LLM 增强
                "meta_source": "ddl_comment" if col_comment else "llm_enhanced",
            }

            # 写入 MySQL
            save_column(key, database_name, table_name, col_name, result[key], samples)

            # 审计事件：新增 or 修改
            log_metadata_changes([{
                "event_type": EVENT_COLUMN_ADDED if col_name in added else EVENT_COLUMN_MODIFIED,
                "database_name": database_name,
                "table_name": table_name,
                "column_name": col_name,
                "detail": {"full_key": key, "col_type": col_type, "original_comment": col_comment},
            }])

            print("✓")

        # 同步该表指纹状态（含仅有表注释变化的场景）
        save_sync_state(database_name, table_name, table_fp, col_fps)

    return result, changed_tables


# ── Step 2: 表级增强（字段反哺表） ─────────────────────────────────────
def _enrich_tables(meta_provider, structured_llm, enriched_columns, changed_tables, force=False):
    """自底向上增强：用字段级增强结果（维度/度量标记+别名）反哺表级元数据
    仅当表不存在、有字段变化（新增/修改/删除）或表注释变化时才重跑 LLM；
    force=True 时强制重跑所有表（用于增强逻辑升级后刷新表级元数据）"""
    # 表级增强需要原始表备注写入 _original_comment，缓存命中时也会补充表备注
    tables = meta_provider.list_tables(with_comment=True)
    result = {}
    total_tables = len(tables)
    # 加载现有表增强结果，用于比较表注释是否变化（变化时也需重跑）
    existing_tables = load_enriched_tables()

    for idx, table_info in enumerate(tables):
        table_name = table_info["table_name"]
        database_name = table_info["database_name"]
        full_name = f"{database_name}.{table_name}"
        new_comment = (table_info.get("table_comment") or "").strip()
        old_comment = (existing_tables.get(full_name, {}).get("original_comment") or "").strip()

        # 断点续跑：表已在 MySQL 中且字段、表注释均未变化 → 跳过（force=True 时强制重跑）
        if not force and table_exists(full_name) and full_name not in changed_tables and old_comment == new_comment:
            print(f"  [{idx+1}/{total_tables}] {full_name} 已存在且字段、表注释无变化，跳过")
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

        prompt = f"""表名：{full_name}
原始注释：{table_info.get("table_comment", "") or "无"}

字段增强信息（含维度/度量标记和业务别名）：
{column_summary}"""

        parsed = _invoke_structured(
            structured_llm, prompt, TABLE_ENRICH_SYSTEM_PROMPT, full_name
        )

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
def _enrich_databases(meta_provider, structured_llm, enriched_tables):
    """自底向上增强：用表级增强结果反哺库级元数据"""
    result = {}
    total_dbs = len(get_allowed_databases())

    for idx, db_name in enumerate(get_allowed_databases()):
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

        prompt = f"""数据库：{db_name}
该库下表的增强信息：
{table_summary}

库下完整表清单（库名.表名）：
{json.dumps(list(db_tables.keys()), ensure_ascii=False)}"""

        parsed = _invoke_structured(
            structured_llm, prompt, DATABASE_ENRICH_SYSTEM_PROMPT, db_name
        )

        result[db_name] = {
            "domain": parsed.get("domain", ""),
            "full_table_list": parsed.get("full_table_list", list(db_tables.keys())),
            "description": parsed.get("description", ""),
        }

        # 写入 MySQL
        save_database(db_name, result[db_name])

        print("✓")

    return result


# ── M3: 表废弃治理（auto_retire_missing=True 时启用） ────────────────────
def _retire_missing_tables(meta_provider):
    """Hive 中已消失的表：清理增强数据与指纹状态，并写入 table_retired 审计事件"""
    from agentTest.db.metadata_scope import get_auto_retire_missing
    if not get_auto_retire_missing():
        return
    current = {(t["database_name"], t["table_name"]) for t in meta_provider.list_tables()}
    for full_name in get_all_sync_states():
        db_name, table_name = full_name.split(".", 1)
        if (db_name, table_name) not in current:
            delete_table_data(full_name)
            delete_sync_state(db_name, table_name)
            log_metadata_changes([{
                "event_type": EVENT_TABLE_RETIRED,
                "database_name": db_name,
                "table_name": table_name,
                "column_name": "",
                "detail": {"full_name": full_name},
            }])
            print(f"  [retire] {full_name} 已从 Hive 消失，清理增强数据与指纹状态")


# ── 主入口 ──────────────────────────────────────────────────────────
def build_enriched_metadata(output_path="metadata/enriched_metadata.json", force_tables=False):
    """主流程：字段→表→库 自底向上串联，写入 MySQL + 本地 JSON 备份
    force_tables=True 时跳过字段级增强，复用 MySQL 字段增强结果并强制重跑表级/库级"""
    # 初始化 MySQL 表结构
    init_metadata_tables()

    meta_provider = HiveMetadataProvider()
    datasource = HiveDataSource()
    # 字段/表/库三级各用独立的结构化 LLM（API 层约束各自的输出 schema）
    column_llm = _build_structured_llm(ColumnEnrichmentOutput)
    table_llm = _build_structured_llm(TableEnrichmentOutput)
    database_llm = _build_structured_llm(DatabaseEnrichmentOutput)

    # Step 0: Hive 中已消失的表按配置治理（M3 table_retired）
    print("Step 0/4: 检查并清理已消失的表...")
    _retire_missing_tables(meta_provider)

    # Step 1: 字段级增强（含采样，schema 指纹增量）
    if force_tables:
        # 强制重跑表级：字段级不重跑，字段上下文从 MySQL 现有增强结果加载
        print("Step 1/4: 跳过字段级增强（复用 MySQL 字段增强结果）...")
        from agentTest.metadata.mysql_store import load_enriched_columns as _load_enriched_columns
        enriched_columns = {c["full_key"]: c for c in _load_enriched_columns()}
        changed_tables = set()
        print(f"  -> 复用 {len(enriched_columns)} 个字段增强结果\n")
    else:
        print("Step 1/4: 增强字段级元数据（含采样）...")
        enriched_columns, changed_tables = _enrich_columns(meta_provider, datasource, column_llm)
        print(f"  -> 完成 {len(enriched_columns)} 个新字段\n")

    # Step 2: 字段 → 表 反哺
    print("Step 2/4: 字段 → 表 反哺增强...")
    enriched_tables = _enrich_tables(meta_provider, table_llm, enriched_columns, changed_tables, force=force_tables)
    print(f"  -> 完成 {len(enriched_tables)} 张表\n")

    # Step 3: 表 → 库 反哺
    print("Step 3/4: 表 → 库 反哺增强...")
    enriched_databases = _enrich_databases(meta_provider, database_llm, enriched_tables)
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
