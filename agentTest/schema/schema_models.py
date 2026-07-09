def build_table_meta(table_name, table_comment):
    return {
        "table_name": table_name,
        "table_comment": table_comment or ""
    }


def build_column_meta(name, data_type, comment):
    return {
        "name": name,
        "type": data_type,
        "comment": comment or ""
    }


def build_table_schema(table_name, table_comment, columns):
    return {
        "table_name": table_name,
        "table_comment": table_comment or "",
        "columns": columns or []
    }
