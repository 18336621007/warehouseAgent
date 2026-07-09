from abc import ABC, abstractmethod


class BaseMetadataProvider(ABC):
    # 元数据提供者抽象基类，统一定义表列表和表结构获取能力

    @abstractmethod
    def list_tables(self):
        # 返回当前数据源下的候选表列表
        raise NotImplementedError

    @abstractmethod
    def describe_table(self, table_name: str):
        # 返回指定表的字段结构信息
        raise NotImplementedError