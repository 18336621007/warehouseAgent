from abc import ABC, abstractmethod


class BaseDataSource(ABC):
    # 数据源抽象基类，统一定义查询与 schema 元数据能力

    @abstractmethod
    def query(self, sql: str):
        # 执行只读 SQL 查询
        raise NotImplementedError

    @abstractmethod
    def list_tables(self):
        # 列出当前数据源中的表信息
        raise NotImplementedError

    @abstractmethod
    def describe_table(self, table_name: str):
        # 获取指定表的字段结构信息
        raise NotImplementedError