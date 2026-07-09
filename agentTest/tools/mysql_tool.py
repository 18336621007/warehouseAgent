from agentTest.db.db_config import get_mysql_config
from agentTest.validate.sql_validate import is_read_only_sql
import pymysql
from decimal import Decimal
from datetime import date, datetime, time

class MySQLTool:
    # 当前阶段先保留类名，内部改为依赖数据源接口

    def __init__(self, datasource):
        self.datasource = datasource

    def run(self, args):

        sql = args.get("sql")
        if not sql:
            raise ValueError("missing sql")

        return self.datasource.query(sql)



def normalize_value(value):
    #Decimal转string
    if isinstance(value, Decimal):
        return str(value)

    # (datetime, date, time)转
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    return value