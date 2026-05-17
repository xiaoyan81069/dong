---
name: skill-finder
description: >-
  技能发现：搜索现有技能、查看技能能力、避免重复造轮子。
  当用户说"有什么技能"、"能做什么"、"会不会XX"、"找个技能"、"查技能"时使用。
metadata:
  dong:
    requires:
      tools: ["search_memory"]
      os: ["win32"]
---

# 技能发现

## 触发条件
- "有什么技能 / 能做什么 / 会不会 / 技能列表 / 找个技能 / 查技能 / 你能干啥"
- "有没有XX技能 / XX功能有吗"
- 用户描述一个需求，不确定是否已有对应技能时

## 步骤
1. 扫描 `dong/skills/` 下所有子目录的 SKILL.md 文件
2. 读取每个 SKILL.md 的 frontmatter（name + description），不读全文（省token）
3. 按用户需求匹配最相关的技能
4. 返回匹配结果：技能名、一句话描述、触发词

## 规则
- 只读 name 和 description 字段，不要加载完整 instructions
- 如果用户需求能匹配到现有技能，直接告诉用户"这个已有：XX技能"
- 如果匹配不到，明确说"目前没有，可以新建"，然后帮用户写新的 SKILL.md
- 搜索词精简到2-3个关键词
- 结果按相关度排序，最多展示5个
