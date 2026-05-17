"""
冬 · 技能自学习器
- agent_loop 成功完成复杂任务后，自动抽象为 SKILL.md
- 存到 dong/skills/learned/ ，下次直接命中
- 去重合并已有技能
"""
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("dong.core.skill_learner")

LEARNED_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "learned"
)


def _task_to_skill_name(task: str) -> str:
    """任务描述 → 技能名"""
    # 保留中文，替换空格和特殊字符
    name = re.sub(r'[^\w\u4e00-\u9fff-]', '-', task.lower())[:40].strip('-')
    return name or "unnamed-skill"


def _extract_stable_steps(history: List[Dict]) -> List[Dict]:
    """从执行历史中提取稳定步骤"""
    steps = []
    for item in history:
        action = item.get("action", "")
        if not action or "retry" in action.lower():
            continue
        steps.append({
            "action": action,
            "tool": item.get("tool", ""),
            "window": item.get("window", ""),
        })
    return steps


def _build_skill_md(task: str, steps: List[Dict], tools_used: List[str]) -> str:
    """生成 SKILL.md 内容"""
    name = _task_to_skill_name(task)
    step_lines = []
    for i, s in enumerate(steps, 1):
        step_lines.append(f"{i}. {s['action']}")

    tools_str = json.dumps(tools_used)
    return f"""---
name: {name}
description: >-
  自动完成任务：{task}。使用工具 {', '.join(tools_used)}。
metadata:
  dong:
    requires:
      tools: {tools_str}
---
# {task}

## 触发条件
- 用户说类似"{task}"时

## 步骤
{chr(10).join(step_lines)}

## 规则
- 不要在未确认的情况下执行破坏性操作
- 如果环境变化导致步骤失败，重新规划而不是强制回放
- 此技能由AI自动生成 (learned {datetime.now().strftime('%Y-%m-%d %H:%M')})
"""


def _check_duplicate(task: str, steps: List[Dict]) -> Optional[str]:
    """检查是否与已有技能重复"""
    if not os.path.isdir(LEARNED_DIR):
        return None

    new_step_text = " ".join(s.get("action", "") for s in steps)
    for entry in os.listdir(LEARNED_DIR):
        skill_md = os.path.join(LEARNED_DIR, entry, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        try:
            with open(skill_md, "r", encoding="utf-8") as f:
                content = f.read()
            # 简单比较：步骤相似度
            existing_steps = re.findall(r'\d+\.\s+(.+)', content)
            existing_text = " ".join(existing_steps)
            # Jaccard 词级别相似
            new_words = set(new_step_text.split())
            old_words = set(existing_text.split())
            if new_words and old_words:
                overlap = len(new_words & old_words) / len(new_words | old_words)
                if overlap > 0.7:
                    return entry  # 太相似了
        except Exception:
            continue
    return None


def save_learned_skill(task: str, history: List[Dict], tools_used: List[str]) -> Optional[str]:
    """
    任务成功后调用，存为技能。
    返回技能名，如果太简单或重复则返回None。
    """
    steps = _extract_stable_steps(history)
    if len(steps) < 3:
        logger.debug("技能跳过: 步骤不足(%d)", len(steps))
        return None

    # 去重
    dup = _check_duplicate(task, steps)
    if dup:
        logger.info("技能跳过: 与 %s 重复", dup)
        return None

    skill_md = _build_skill_md(task, steps, tools_used)
    skill_name = _task_to_skill_name(task)
    skill_dir = os.path.join(LEARNED_DIR, skill_name)

    os.makedirs(LEARNED_DIR, exist_ok=True)
    # 如果已存在同名，加版本号
    if os.path.isdir(skill_dir):
        skill_dir = os.path.join(LEARNED_DIR, f"{skill_name}-v2")
    os.makedirs(skill_dir, exist_ok=True)

    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(skill_md)

    logger.info("新技能已保存: %s (%d步)", skill_name, len(steps))
    return skill_name
