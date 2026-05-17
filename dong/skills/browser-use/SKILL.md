---
name: browser-use
description: >-
  浏览器自动化：打开网页、填表、截图、抓取数据。
  当用户说"打开浏览器"、"帮我查网页"、"登录XX"时使用。
metadata:
  dong:
    requires:
      tools: ["computer_control"]
      os: ["win32"]
---

# 浏览器自动化

## 触发条件
- 用户提到"浏览器/网页/网站/查一下/登录/下单/截图网页"

## 步骤
1. 用 computer_control `action=launch` 打开浏览器（Chrome/Edge）
2. 用 `action=click_element` 或 `action=click(x,y)` 点击页面元素
3. 用 `action=type` 输入文字（表单/搜索框）
4. 用 `action=screenshot` 截图确认结果
5. 需要读取页面内容时，用 `action=analyze` 让VLM描述

## 规则
- 不要在未确认的情况下提交支付/删除操作
- 涉及登录时，先提示用户确认
- 页面加载慢时等待2-3秒再操作
- 如果元素找不到，截图后用VLM重新定位
