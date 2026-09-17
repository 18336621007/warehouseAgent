# 多源元数据提供者：主通道 Hive（表清单/字段结构），Hive 查不到时兜底语义层 physical
# 查询执行的多引擎路由见 datasource/registry.py；元数据统一经此入口，避免各工具各自兜底
from agentTest.metadata.base_metadata_provider import BaseMetadataProvider


class MultiSourceMetadataProvider(BaseMetadataProvider):
    # 包装主元数据源 + 兜底源（语义层 physical），describe 失败时自动降级

    def __init__(self, primary: BaseMetadataProvider, fallback: BaseMetadataProvider):
        self._primary = primary
        self._fallback = fallback

    def list_tables(self, with_comment: bool = False):
        # 表清单仍以主源（Hive）为准
        return self._primary.list_tables(with_comment=with_comment)

    def describe_table(self, table_identifier: str):
        # 主源查不到（如 Hive 无 data_project 库）时降级兜底源（语义层 physical）
        try:
            return self._primary.describe_table(table_identifier)
        except Exception:
            return self._fallback.describe_table(table_identifier)
