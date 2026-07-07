import sqlite3
"""初始化模拟数据"""
conn = sqlite3.connect("test.db")
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER,
    amount INTEGER
)
''')

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER,
    name TEXT
)
""")

cursor.execute("DELETE FROM orders")
cursor.execute("DELETE FROM users")

cursor.executemany(
    "INSERT INTO orders VALUES (?, ?)",
    [(1, 100), (2, 200)]
)

conn.commit()
conn.close()