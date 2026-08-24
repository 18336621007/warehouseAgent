# 元数据增强 LLM 结构化输出的 Pydantic 模型 + Prompt 模板
# 参考 planner_prompt：用 with_structured_output 在 API 层约束输出格式，
# 避免仅靠 prompt 声明 JSON 导致字段缺失 / 数组为空。
from typing import Literal

from pydantic import BaseModel, Field


class ColumnEnrichmentOutput(BaseModel):
    """字段级增强输出：领域 / 维度度量标记 / 关系 / 业务别名"""

    domain: str = Field(
        default="",
        description="该字段所属业务领域（与表保持一致）"
    )

    fields_type: Literal["dimension", "measure"] = Field(
        default="dimension",
        description="dimension=分组维度（状态/平台/渠道/日期等），measure=可聚合数值（金额/数量/天数等）"
    )

    relations: list[str] = Field(
        default_factory=list,
        description="字段关联关系，本阶段固定为空数组"
    )

    field_aliases: list[str] = Field(
        default_factory=list,
        description="字段的中文同义词或业务别名，根据字段名推断给出 1-3 个，字段名本身可作为别名"
    )


class TableEnrichmentOutput(BaseModel):
    """表级增强输出：领域 / 核心功能 / 关键实体 / 潜在分析场景"""

    domain: str = Field(
        default="",
        description="该表所属业务领域的中文标签"
    )

    core_function: str = Field(
        default="",
        description="综合字段的维度/度量标记和别名，用一段话描述该表的核心功能和存储内容"
    )

    key_entities: list[str] = Field(
        default_factory=list,
        description="核心实体字段名（如主键、业务主键），至少 1 个"
    )

    potential_use_cases: list[str] = Field(
        default_factory=list,
        description="依据 measure 字段推断的适用分析场景，至少给出 2-3 个，禁止为空"
    )


class DatabaseEnrichmentOutput(BaseModel):
    """库级增强输出：领域 / 完整表清单 / 描述"""

    domain: str = Field(
        default="",
        description="该库所属业务领域的中文标签"
    )

    full_table_list: list[str] = Field(
        default_factory=list,
        description="该库下的完整表清单（库名.表名），必须完整列出，禁止遗漏"
    )

    description: str = Field(
        default="",
        description="综合该库下所有表的 core_function，用一段话概括该库的数据定位和核心价值"
    )


COLUMN_ENRICH_SYSTEM_PROMPT = """你是数据仓库元数据专家，负责为单个字段生成增强元数据。
必须输出结构化字段：domain、fields_type、relations、field_aliases。
要求：
- fields_type 只能是 dimension 或 measure
- field_aliases 根据字段名推断 1-3 个中文同义词或业务别名，字段名本身可作为别名
- relations 固定为空数组
- 禁止编造字段名和原始注释中不存在的信息"""


TABLE_ENRICH_SYSTEM_PROMPT = """你是数据仓库元数据专家，负责为 Hive 表生成表级增强元数据。
必须输出结构化字段：domain、core_function、key_entities、potential_use_cases。
要求：
- core_function 要体现字段增强信息，如"存储订单明细，包含【度量】金额/天数等可聚合指标和【维度】状态/平台等分组维度"
- potential_use_cases 必须依据 measure 字段推断，至少给出 2-3 个分析场景，禁止为空
- key_entities 至少给出 1 个核心实体字段
- 禁止编造表结构和字段中不存在的信息"""


DATABASE_ENRICH_SYSTEM_PROMPT = """你是数据仓库元数据专家，负责为数据库生成库级增强元数据。
必须输出结构化字段：domain、full_table_list、description。
要求：
- full_table_list 必须完整列出给定库下的所有表（库名.表名），禁止遗漏
- description 要综合所有表的 core_function，用一段话概括数据定位和核心价值"""
