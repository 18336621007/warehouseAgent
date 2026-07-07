import time
import random
import sqlite3
import os


class MySQLTool:

    def run(self, args):

        # 1.获取参数中的sql
        sql = args.get("sql")
        if not sql:
            raise Exception(f"[MySQLTool] 缺少sql参数")

        # 2.建立连接
        conn = sqlite3.connect('D:\\code\\Project\\test\\agentTest\\db\\test.db')
        cursor = conn.cursor()
        print(f"[MySQLTool] connect to {sql}")

        #3. 执行sql
        cursor.execute(sql)# 执行sql
        rows = cursor.fetchall() #取出查询的全部行数据，返回元组列表

        # cursor.description会返回查询字段的元组信息，desc[0]取出每一列的字段名，得到字段名列表["id","name"]
        columns = [desc[0] for desc in cursor.description]


        result = [
            dict(zip(columns, row)) #zip(字段列表，单行数据)，变成[{"id":1, "name":"张三"}]
            for row in rows
        ]

        conn.close()

        return result

