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

        # # 1.获取参数中的sql
        # sql = args.get("sql")
        # isOK, msg = is_read_only_sql(sql)
        # if not sql:
        #     raise Exception(f"[MySQLTool] 缺少sql参数")
        # if not isOK:
        #     raise Exception(f"[MySQLTool] {msg}")
        #
        # # 2.建立连接
        # db_cfg = get_mysql_config()
        # conn = pymysql.connect(
        #     host=db_cfg["host"],
        #     port=db_cfg["port"],
        #     user=db_cfg["user"],
        #     password=db_cfg["password"],
        #     database=db_cfg["database"],
        #     charset=db_cfg["charset"]
        # )
        # cursor = conn.cursor()
        #
        # try:
        #     # 3. 执行查询SQL
        #     cursor.execute(sql)
        #     # 获取字段名
        #     columns = [col[0] for col in cursor.description]
        #     # 获取所有数据行
        #     rows = cursor.fetchall()
        #
        #     result_rows = []
        #     for row in rows:
        #         row_dict = {}
        #         for column, value in zip(columns, row):
        #             row_dict[column] = normalize_value(value)
        #         result_rows.append(row_dict)
        #
        #     result = {
        #         "columns": columns,
        #         "rows": result_rows,
        #         "row_count": len(result_rows),
        #     }
        # finally:
        #     # 无论是否报错，都关闭游标与连接
        #     cursor.close()
        #     conn.close()
        #
        # return result

def normalize_value(value):
    #Decimal转string
    if isinstance(value, Decimal):
        return str(value)

    # (datetime, date, time)转
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    return value