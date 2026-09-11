# 通用 skill 机制：扫描 skills/ 目录、解析 SKILL.md frontmatter、关键词匹配、格式化注入文本
#
# skill 是"决策策略层"：程序只解析 frontmatter（name/description/trigger_keywords/scope），
# 正文业务规则全部由 LLM 读取执行；不替代语义层/RAG/落盘结果/工具，只决定"这个场景怎么用它们"。
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass
class SkillSpec:
    """一个 skill 的元信息与指令正文。"""
    name: str
    description: str
    version: str
    trigger_keywords: List[str] = field(default_factory=list)
    instruction_text: str = ""
    references_dir: str = ""
    scope: List[str] = field(default_factory=lambda: ["planner"])


def _parse_skill_md(path: Path) -> Optional[SkillSpec]:
    """解析单个 SKILL.md：--- frontmatter 块 + 正文指令。"""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return None
    name = str(meta.get("name") or "").strip()
    if not name:
        return None
    return SkillSpec(
        name=name,
        description=str(meta.get("description") or "").strip(),
        version=str(meta.get("version") or "").strip(),
        trigger_keywords=[
            str(k).strip() for k in (meta.get("trigger_keywords") or []) if str(k).strip()
        ],
        instruction_text=m.group(2).strip(),
        references_dir=str(path.parent / "references"),
        scope=[
            str(s).strip() for s in (meta.get("scope") or ["planner"]) if str(s).strip()
        ],
    )


class SkillManager:
    """通用 skill 管理器：加载、匹配、格式化注入文本。"""

    def __init__(self, root: str = ""):
        # 默认根目录：agentTest/skills（skill_loader.py 位于 agentTest/langgraph_app/skills/）
        self.root = Path(root) if root else Path(__file__).resolve().parents[2] / "skills"
        self._skills: List[SkillSpec] = []
        self.reload()

    def reload(self) -> None:
        """重新扫描 skills/ 目录，加载所有合法 SKILL.md。"""
        self._skills = []
        if not self.root.exists():
            return
        for skill_md in sorted(self.root.glob("*/SKILL.md")):
            spec = _parse_skill_md(skill_md)
            if spec:
                self._skills.append(spec)

    @property
    def skills(self) -> List[SkillSpec]:
        return list(self._skills)

    def match_skills(self, question: str, scope: str = "planner") -> List[SkillSpec]:
        """按 trigger_keywords + description 关键词匹配，命中数降序；零 LLM、可审计。"""
        question = str(question or "")
        if not question:
            return []
        scored = []
        for spec in self._skills:
            if scope and spec.scope and scope not in spec.scope:
                continue
            hits = 0
            for kw in spec.trigger_keywords:
                if kw and kw in question:
                    hits += 1
            # description 子串匹配：question 整体命中 description，或 description 任一分词命中 question
            desc = spec.description or ""
            desc_words = [
                w for w in re.split(r"[\s,，、。;；:：]+", desc)
                if len(w) >= 2
            ]
            if question in desc or any(w in question for w in desc_words):
                hits += 1
            if hits > 0:
                scored.append((hits, spec))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored]

    def format_instruction(self, question: str, scope: str = "planner") -> str:
        """把命中 skill 拼成注入文本（含 references 目录路径供 LLM 按需读取）。"""
        matched = self.match_skills(question, scope=scope)
        if not matched:
            return ""
        parts = []
        for spec in matched:
            block = f"【技能：{spec.name}】\n{spec.instruction_text}"
            if Path(spec.references_dir).exists():
                block += f"\n\n技能资源目录（按需读取）：{spec.references_dir}"
            parts.append(block)
        return "\n\n".join(parts)


def build_skill_manager(root: str = "") -> SkillManager:
    """构建 skill 管理器（runtime 注册入口）。"""
    return SkillManager(root=root)
