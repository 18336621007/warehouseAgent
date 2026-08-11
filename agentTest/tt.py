import dotenv

from agentTest.metadata.hive_meta_provider import HiveMetadataProvider
dotenv.load_dotenv()
p = HiveMetadataProvider()
tables = p.list_tables()
print("count:", len(tables))
for t in tables:
    print(t["database_name"], t["table_name"])
print("found:", [t for t in tables if "dimension_rent" in t["table_name"]])