# schema_fingerprint 单元测试。
# 覆盖：字段指纹确定性/敏感性、表指纹字段顺序不敏感、字段增删改与表注释变化触发、
# diff_columns 的 added/modified/removed 判定，以及分隔符防撞。
import unittest

from agentTest.metadata.schema_fingerprint import (
    column_fingerprint,
    diff_columns,
    table_fingerprint,
)


class TestColumnFingerprint(unittest.TestCase):
    """字段指纹：sha1(字段名|类型|原始注释) 的确定性、敏感性与防撞。"""

    def test_deterministic(self):
        # 相同输入必须得到相同指纹
        fp1 = column_fingerprint("company_id", "bigint", "经销商ID")
        fp2 = column_fingerprint("company_id", "bigint", "经销商ID")
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 40)  # sha1 hexdigest 长度

    def test_differs_when_name_changes(self):
        # 字段名变化 → 指纹变化
        self.assertNotEqual(
            column_fingerprint("company_id", "bigint", "经销商ID"),
            column_fingerprint("company_name", "bigint", "经销商ID"),
        )

    def test_differs_when_type_changes(self):
        # 类型变化 → 指纹变化
        self.assertNotEqual(
            column_fingerprint("company_id", "bigint", "经销商ID"),
            column_fingerprint("company_id", "string", "经销商ID"),
        )

    def test_differs_when_comment_changes(self):
        # 注释变化 → 指纹变化
        self.assertNotEqual(
            column_fingerprint("company_id", "bigint", "经销商ID"),
            column_fingerprint("company_id", "bigint", "经销商编号"),
        )

    def test_pipe_separator_prevents_collision(self):
        # “a|b”作为字段名 与 字段名a类型b 不得撞指纹
        fp1 = column_fingerprint("a|b", "", "")
        fp2 = column_fingerprint("a", "b", "")
        self.assertNotEqual(fp1, fp2)

    def test_empty_parts_ok(self):
        # 空字段名/类型/注释均可计算，不抛异常
        self.assertTrue(column_fingerprint("", "", ""))


class TestTableFingerprint(unittest.TestCase):
    """表指纹：sha1(字段指纹映射JSON + 表注释)，字段顺序不敏感。"""

    @staticmethod
    def _cols(names_types_comments):
        # 把 [(name, type, comment), ...] 转成 column_fingerprint 所需的 dict 列表
        return [
            {"name": name, "type": type_, "comment": comment}
            for name, type_, comment in names_types_comments
        ]

    def test_field_order_insensitive(self):
        # 字段声明顺序变化不影响表指纹
        cols_a = self._cols([("a", "string", "注释A"), ("b", "bigint", "注释B")])
        cols_b = self._cols([("b", "bigint", "注释B"), ("a", "string", "注释A")])
        fp_a, _ = table_fingerprint(cols_a)
        fp_b, _ = table_fingerprint(cols_b)
        self.assertEqual(fp_a, fp_b)

    def test_add_column_changes_fingerprint(self):
        # 新增字段 → 表指纹变化
        cols_base = self._cols([("a", "string", "注释A")])
        cols_more = self._cols([("a", "string", "注释A"), ("b", "bigint", "注释B")])
        fp_base, _ = table_fingerprint(cols_base)
        fp_more, _ = table_fingerprint(cols_more)
        self.assertNotEqual(fp_base, fp_more)

    def test_remove_column_changes_fingerprint(self):
        # 删除字段 → 表指纹变化
        cols_full = self._cols([("a", "string", "注释A"), ("b", "bigint", "注释B")])
        cols_less = self._cols([("a", "string", "注释A")])
        fp_full, _ = table_fingerprint(cols_full)
        fp_less, _ = table_fingerprint(cols_less)
        self.assertNotEqual(fp_full, fp_less)

    def test_modify_column_changes_fingerprint(self):
        # 字段类型/注释变化 → 表指纹变化
        cols_old = self._cols([("a", "string", "注释A")])
        cols_new = self._cols([("a", "bigint", "注释A")])
        fp_old, _ = table_fingerprint(cols_old)
        fp_new, _ = table_fingerprint(cols_new)
        self.assertNotEqual(fp_old, fp_new)

    def test_table_comment_changes_fingerprint(self):
        # 表注释变化 → 表指纹变化
        cols = self._cols([("a", "string", "注释A")])
        fp1, _ = table_fingerprint(cols, table_comment="表注释1")
        fp2, _ = table_fingerprint(cols, table_comment="表注释2")
        self.assertNotEqual(fp1, fp2)

    def test_column_fps_map_returned(self):
        # 返回的字段指纹映射应覆盖全部字段
        cols = self._cols([("a", "string", "注释A"), ("b", "bigint", "注释B")])
        _, col_fps = table_fingerprint(cols)
        self.assertEqual(set(col_fps), {"a", "b"})


class TestDiffColumns(unittest.TestCase):
    """字段级 diff：added / modified / removed 三组判定。"""

    def test_no_change(self):
        # 无变化 → 三组均为空
        prev = {"a": "fp-a", "b": "fp-b"}
        curr = {"a": "fp-a", "b": "fp-b"}
        added, modified, removed = diff_columns(prev, curr)
        self.assertEqual((added, modified, removed), ([], [], []))

    def test_added(self):
        # 仅在当前存在 → added
        prev = {"a": "fp-a"}
        curr = {"a": "fp-a", "b": "fp-b"}
        added, modified, removed = diff_columns(prev, curr)
        self.assertEqual(added, ["b"])
        self.assertEqual(modified, [])
        self.assertEqual(removed, [])

    def test_removed(self):
        # 仅在上次存在 → removed
        prev = {"a": "fp-a", "b": "fp-b"}
        curr = {"a": "fp-a"}
        added, modified, removed = diff_columns(prev, curr)
        self.assertEqual(added, [])
        self.assertEqual(modified, [])
        self.assertEqual(removed, ["b"])

    def test_modified(self):
        # 两次都存在但指纹不同 → modified
        prev = {"a": "fp-a-old", "b": "fp-b"}
        curr = {"a": "fp-a-new", "b": "fp-b"}
        added, modified, removed = diff_columns(prev, curr)
        self.assertEqual(added, [])
        self.assertEqual(modified, ["a"])
        self.assertEqual(removed, [])

    def test_mixed(self):
        # 同时存在新增/修改/删除
        prev = {"a": "fp-a-old", "b": "fp-b", "c": "fp-c"}
        curr = {"a": "fp-a-new", "b": "fp-b", "d": "fp-d"}
        added, modified, removed = diff_columns(prev, curr)
        self.assertEqual(sorted(added), ["d"])
        self.assertEqual(modified, ["a"])
        self.assertEqual(sorted(removed), ["c"])


if __name__ == "__main__":
    unittest.main()
