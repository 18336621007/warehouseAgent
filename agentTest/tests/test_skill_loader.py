# 通用 skill 机制单测：frontmatter 解析 / 关键词匹配 / 注入文本格式化 / scope 过滤
import tempfile
import unittest
from pathlib import Path

from agentTest.langgraph_app.skills.skill_loader import build_skill_manager


def _write_skill(root: Path, name: str, body: str) -> Path:
    """写入一个临时 SKILL.md 并返回路径。"""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


class TestSkillLoader(unittest.TestCase):
    """skill_loader 加载/匹配/格式化基础能力。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_parse_frontmatter(self):
        _write_skill(self.root, "demo", (
            "---\n"
            "name: demo\n"
            "description: 演示 skill\n"
            "version: 1.0.0\n"
            "trigger_keywords: [查一下, 统计]\n"
            "scope: [planner]\n"
            "---\n"
            "## 指令\n演示正文\n"
        ))
        mgr = build_skill_manager(str(self.root))
        self.assertEqual(len(mgr.skills), 1)
        spec = mgr.skills[0]
        self.assertEqual(spec.name, "demo")
        self.assertEqual(spec.version, "1.0.0")
        self.assertEqual(spec.trigger_keywords, ["查一下", "统计"])
        self.assertIn("演示正文", spec.instruction_text)

    def test_match_keyword_and_description(self):
        _write_skill(self.root, "demo", (
            "---\nname: demo\ndescription: 离线数仓数据查询\n"
            "trigger_keywords: [查一下, 统计]\n---\n指令\n"
        ))
        mgr = build_skill_manager(str(self.root))
        # 触发词命中
        self.assertEqual([s.name for s in mgr.match_skills("查一下今天的订单")], ["demo"])
        # description 分词命中
        self.assertEqual([s.name for s in mgr.match_skills("数仓")], ["demo"])
        # 无关输入不命中
        self.assertEqual(mgr.match_skills("今天天气"), [])

    def test_format_instruction(self):
        _write_skill(self.root, "demo", (
            "---\nname: demo\ndescription: 演示\n---\n## 行为指令\n按规则执行\n"
        ))
        mgr = build_skill_manager(str(self.root))
        text = mgr.format_instruction("演示一下")
        self.assertIn("【技能：demo】", text)
        self.assertIn("按规则执行", text)

    def test_scope_filter(self):
        _write_skill(self.root, "demo", (
            "---\nname: demo\ndescription: 演示\nscope: [advisor]\n---\n指令\n"
        ))
        mgr = build_skill_manager(str(self.root))
        # scope 不含 planner → planner 视角不命中
        self.assertEqual(mgr.match_skills("演示", scope="planner"), [])
        # advisor 视角命中
        self.assertEqual(len(mgr.match_skills("演示", scope="advisor")), 1)

    def test_no_skills_dir(self):
        empty = self.root / "nope"
        mgr = build_skill_manager(str(empty))
        self.assertEqual(mgr.skills, [])
        self.assertEqual(mgr.format_instruction("查一下"), "")

    def test_invalid_skill_md(self):
        _write_skill(self.root, "bad", "no frontmatter here\n")
        mgr = build_skill_manager(str(self.root))
        self.assertEqual(mgr.skills, [])


if __name__ == "__main__":
    unittest.main()
