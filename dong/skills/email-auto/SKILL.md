---
name: email-auto
description: >-
  邮件管家：查看收件箱、发送邮件、处理邮件通知。
  当用户说"帮我查邮件"、"发邮件给XX"、"看看有没有新邮件"时使用。
metadata:
  dong:
    requires:
      tools: ["computer_control"]
      os: ["win32"]
---

# 邮件管家

## 触发条件
- "查邮件 / 收件箱 / 新邮件 / 发邮件 / 回复邮件 / 有没有人找我"

## 步骤
1. 用 computer_control `action=launch` 打开邮件客户端（Outlook/网页邮箱）
2. 用 `action=screenshot` + `action=analyze` 查看收件箱内容
3. 如需发件：用 `action=click_element` 点"新建邮件"
4. 用 `action=type` 输入收件人/主题/正文
5. 发送前截图确认，待用户确认后点击发送

## 规则
- 发送前必须让用户确认内容
- 不要自动回复任何邮件
- 只读操作可以直接执行，写操作需要确认
