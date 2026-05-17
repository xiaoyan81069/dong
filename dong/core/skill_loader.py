"""
冬 · 技能加载器 —— AgentSkills 标准兼容
- 启动时只加载 name + description（渐进式披露第一步）
- 匹配到技能时才加载 SKILL.md 全文
- 生成技能清单注入系统提示词
"""
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("dong.core.skill_loader")

SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")


@dataclass
class DongSkill:
    name: str
    description: str
    requires_tools: List[str] = field(default_factory=list)
    requires_os: List[str] = field(default_factory=list)
    instructions: str = ""       # SKILL.md 正文（渐进加载）
    metadata: Dict = field(default_factory=dict)
    source: str = ""             # "bundled" / "learned"


def _parse_skill_md(filepath: str) -> Optional[DongSkill]:
    """解析单个 SKILL.md 文件"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None

    frontmatter = {}
    body = ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 2:
            # 简易 YAML 解析（避免依赖 pyyaml），支持多行值和简单嵌套
            lines = parts[1].strip().split("\n")
            i = 0
            _last_key = ""
            while i < len(lines):
                line = lines[i].rstrip()
                if not line.strip():
                    i += 1
                    continue
                # 多行值续行（以空格开头且不含顶级冒号）
                if line[0] in (' ', '\t') and ':' not in line:
                    if _last_key:
                        prev = frontmatter.get(_last_key, "")
                        frontmatter[_last_key] = prev + " " + line.strip()
                    i += 1
                    continue
                if ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip()
                    # 多行折叠标记 >- 或 | 等
                    if val in ('>-', '|', '>', '|-', '>+'):
                        # 收集后续缩进的行作为值
                        collected = []
                        j = i + 1
                        while j < len(lines) and (not lines[j].strip() or lines[j][0] in (' ', '\t')):
                            if lines[j].strip():
                                collected.append(lines[j].strip())
                            j += 1
                        frontmatter[key] = ' '.join(collected)
                        i = j
                        _last_key = key
                        continue
                    frontmatter[key] = val.strip('"').strip("'")
                    _last_key = key
                i += 1
            body = parts[2].strip() if len(parts) > 2 else ""
    else:
        body = text

    name = frontmatter.get("name", "")
    if not name:
        return None

    # 解析需要工具（从 metadata.dong.requires.tools）
    req_tools = []
    metadata_str = frontmatter.get("metadata", "")
    if metadata_str and "tools" in metadata_str:
        m = re.findall(r'"tools"\s*:\s*\[([^\]]*)\]', metadata_str)
        if m:
            req_tools = [t.strip().strip('"').strip("'") for t in m[0].split(",") if t.strip()]

    return DongSkill(
        name=name,
        description=frontmatter.get("description", ""),
        requires_tools=req_tools,
        requires_os=[],
        instructions=body,
        metadata=frontmatter,
        source="bundled",
    )


def _get_skill_source(skill_dir: str) -> str:
    """判断技能来源"""
    if "learned" in skill_dir:
        return "learned"
    return "bundled"


def load_skills(load_full: bool = False) -> Dict[str, DongSkill]:
    """
    加载所有技能。
    load_full=False: 只加载 name + description（启动时用）
    load_full=True:  加载完整 instructions（匹配到技能时用）
    """
    skills = {}
    if not os.path.isdir(SKILL_DIR):
        return skills

    for entry in os.listdir(SKILL_DIR):
        skill_dir = os.path.join(SKILL_DIR, entry)
        if not os.path.isdir(skill_dir):
            continue
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue

        skill = _parse_skill_md(skill_md)
        if skill is None:
            continue

        skill.source = _get_skill_source(skill_dir)

        if not load_full:
            # 渐进式披露：启动时清空正文，匹配时才加载
            skill.instructions = ""

        skills[skill.name] = skill

    logger.info("加载 %d 个技能 (full=%s)", len(skills), load_full)
    return skills


def load_skill_full(name: str) -> Optional[DongSkill]:
    """按名称加载单个技能的完整内容"""
    for entry in os.listdir(SKILL_DIR):
        skill_dir = os.path.join(SKILL_DIR, entry)
        if not os.path.isdir(skill_dir):
            continue
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        skill = _parse_skill_md(skill_md)
        if skill and skill.name == name:
            skill.source = _get_skill_source(skill_dir)
            return skill
    return None


def build_skills_prompt(skills: Dict[str, DongSkill]) -> str:
    """生成系统提示词中的技能列表（只含 name + description）"""
    if not skills:
        return ""

    bundled = [s for s in skills.values() if s.source == "bundled"]
    learned = [s for s in skills.values() if s.source == "learned"]

    lines = ["【可用技能】"]
    if bundled:
        lines.append("内置技能：")
        for s in bundled:
            lines.append(f"  - {s.name}: {s.description}")
    if learned:
        lines.append("已学技能：")
        for s in learned:
            lines.append(f"  - {s.name}: {s.description}")
    return "\n".join(lines)


def match_skill(task_desc: str, skills: Dict[str, DongSkill]) -> Optional[DongSkill]:
    """
    简单关键词匹配：用任务描述匹配技能 description。
    后续可升级为 embedding 语义匹配。
    """
    task_lower = task_desc.lower()
    best = None
    best_score = 0

    for skill in skills.values():
        score = 0
        desc_lower = skill.description.lower()
        # 关键词命中
        for word in task_lower.split():
            if len(word) >= 2 and word in desc_lower:
                score += 1
        # 完整短语命中
        if task_lower[:15] in desc_lower or desc_lower[:15] in task_lower:
            score += 3
        if score > best_score:
            best_score = score
            best = skill

    if best and best_score >= 2:
        logger.info("技能匹配: %s (score=%d)", best.name, best_score)
        return best
    return None
